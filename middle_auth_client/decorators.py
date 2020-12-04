from functools import wraps
import flask
import json
import os
from urllib.parse import quote
from furl import furl
import cachetools.func
import requests

AUTH_URI = os.environ.get('AUTH_URI', 'localhost:5000/auth')
AUTH_URL = os.environ.get('AUTH_URL', AUTH_URI)
INFO_URL = os.environ.get('INFO_URL', 'localhost:5000/info')
STICKY_AUTH_URL = os.environ.get('STICKY_AUTH_URL', AUTH_URL)

USE_REDIS = os.environ.get('AUTH_USE_REDIS', "false") == "true"
TOKEN_NAME = os.environ.get('TOKEN_NAME', "middle_auth_token")
CACHE_MAXSIZE = int(os.environ.get('TOKEN_CACHE_MAXSIZE', "1024"))
CACHE_TTL = int(os.environ.get('TOKEN_CACHE_TTL', "300"))

r = None
if USE_REDIS:
    import redis
    r = redis.Redis(
        host=os.environ.get('REDISHOST', 'localhost'),
        port=int(os.environ.get('REDISPORT', 6379)))

class AuthorizationError(Exception):
    pass

def get_usernames(user_ids, token=None):
    if token is None:
        raise ValueError('missing token')
    if len(user_ids):
        users_request = requests.get(f"https://{AUTH_URL}/api/v1/username?id={','.join(map(str, user_ids))}",
            headers={'authorization': 'Bearer ' + token},
            timeout=5)

        if users_request.status_code in [401, 403]:
            raise AuthorizationError(users_request.text)
        elif users_request.status_code == 200:
            id_to_name = {x['id']: x['name'] for x in users_request.json()}
            return [id_to_name[x] for x in user_ids]
        else:
            raise RuntimeError('get_usernames request failed')
    else:
        return []

@cachetools.func.ttl_cache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
def user_cache_http(token):
    user_request = requests.get(f"https://{AUTH_URL}/api/v1/user/cache", headers={'authorization': 'Bearer ' + token})
    if user_request.status_code == 200:
        return user_request.json()

def get_user_cache(token):
    if USE_REDIS:
        cached_user_data = r.get("token_" + token)
        if cached_user_data:
            return json.loads(cached_user_data.decode('utf-8'))
    else:
        return user_cache_http(token)

@cachetools.func.ttl_cache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
def is_root_public(table_id, root_id, token):
    if root_id is None:
        return False

    url = f"https://{AUTH_URL}/api/v1/table/{table_id}/root/{root_id}/is_public"

    req = requests.get(url, headers={'authorization': 'Bearer ' + token}, timeout=5)

    if req.status_code == 200:
        return req.json()
    else:
        raise RuntimeError('is_root_public request failed')

@cachetools.func.ttl_cache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
def table_has_public(table_id, token):
    url = f"https://{AUTH_URL}/api/v1/table/{table_id}/has_public"

    req = requests.get(url, headers={'authorization': 'Bearer ' + token}, timeout=5)
    if req.status_code == 200:
        return req.json()
    else:
        raise RuntimeError('has_public request failed')

@cachetools.func.ttl_cache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
def dataset_from_service(service, table_id, token):
    url = f"https://{INFO_URL}/api/v2/tablemapping/service/{service}/table/{table_id}/permission_group"
    req = requests.get(url, headers={'authorization': 'Bearer ' + token}, timeout=5)
    if req.status_code == 200:
        return req.json()
    else:
        raise RuntimeError(f'failed to lookup dataset for service {service} & table_id: {table_id}: status code {req.status_code}. content: {req.content}')

def auth_required(func=None, *, required_permission=None, public_table_key=None, public_node_key=None, service_token=None):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if flask.request.method == 'OPTIONS':
                return f(*args, **kwargs)

            if hasattr(flask.g, 'auth_token'):
                # if authorization header has already been parsed, don't need to re-parse
                # this allows auth_required to be an optional decorator if auth_requires_role is also used
                return f(*args, **kwargs)

            token = None
            cookie_name = TOKEN_NAME

            auth_header = flask.request.headers.get('authorization')
            xrw_header = flask.request.headers.get('X-Requested-With')

            programmatic_access = xrw_header or auth_header or flask.request.environ.get('HTTP_ORIGIN')

            AUTHORIZE_URI = 'https://' + STICKY_AUTH_URL + '/api/v1/authorize'

            query_param_token = flask.request.args.get(TOKEN_NAME)

            if not query_param_token:
                # deprecated
                query_param_token = flask.request.args.get('token')

            flask.g.public_access_cache = None
            def lazy_check_public_access():
                if flask.g.public_access_cache is None:
                    if service_token and required_permission is not 'edit':
                        if public_node_key is not None:
                            flask.g.public_access_cache = is_root_public(kwargs.get(public_table_key), kwargs.get(public_node_key), service_token)
                        elif public_table_key is not None:
                            flask.g.public_access_cache = table_has_public(kwargs.get(public_table_key), service_token)
                        else:
                            flask.g.public_access_cache = False
                    else:
                        flask.g.public_access_cache = False

                return flask.g.public_access_cache

            flask.g.public_access = lazy_check_public_access

            if programmatic_access:
                if query_param_token:
                    token = query_param_token
                else:
                    if not auth_header:
                        if not flask.g.public_access():
                            resp = flask.Response("Unauthorized", 401)
                            resp.headers['WWW-Authenticate'] = 'Bearer realm="' + AUTHORIZE_URI + '"'
                            return resp
                    elif not auth_header.startswith('Bearer '):
                        resp = flask.Response("Invalid Request", 400)
                        resp.headers['WWW-Authenticate'] = 'Bearer realm="' + AUTHORIZE_URI + '", error="invalid_request", error_description="Header must begin with \'Bearer\'"'
                        return resp
                    else:
                        token = auth_header.split(' ')[1] # remove schema
            else: # direct browser access, or a non-browser request missing auth header (user error) TODO: check user agent to deliver 401 in this case
                if query_param_token:
                    resp = flask.make_response(flask.redirect(furl(flask.request.url).remove([TOKEN_NAME, 'token']).url, code=302))
                    resp.set_cookie(cookie_name, query_param_token, secure=True, httponly=True)
                    return resp

                token = flask.request.cookies.get(cookie_name)

            cached_user_data = get_user_cache(token) if token else None

            if cached_user_data:
                flask.g.auth_user = cached_user_data
                flask.g.auth_token = token
                return f(*args, **kwargs)
            elif not programmatic_access and not flask.g.public_access():
                return flask.redirect(AUTHORIZE_URI + '?redirect=' + quote(flask.request.url), code=302)
            elif not flask.g.public_access():
                resp = flask.Response("Invalid/Expired Token", 401)
                resp.headers['WWW-Authenticate'] = 'Bearer realm="' + AUTHORIZE_URI + '", error="invalid_token", error_description="Invalid/Expired Token"'
                return resp
            else:
                flask.g.auth_user = {'id': 0, 'service_account': False, 'name': '', 'email': '', 'admin': False, 'groups': [], 'permissions': {}}
                flask.g.auth_token = None
                return f(*args, **kwargs)

        return decorated_function

    if func:
        return decorator(func)
    else:
        return decorator

def auth_requires_admin(f):
    @wraps(f)
    @auth_required
    def decorated_function(*args, **kwargs):
        if flask.request.method == 'OPTIONS':
            return f(*args, **kwargs)

        if not flask.g.auth_user['admin']:
            resp = flask.Response("Requires superadmin privilege.", 403)
            return resp
        else:
            return f(*args, **kwargs)

    return decorated_function

def auth_requires_permission(required_permission, public_table_key=None, public_node_key=None, service_token=None):
    def decorator(f):
        @wraps(f)
        @auth_required(required_permission=required_permission, public_table_key=public_table_key, public_node_key=public_node_key, service_token=service_token)
        def decorated_function(table_id, *args, **kwargs):
            if flask.request.method == 'OPTIONS':
                return f(*args, **{**kwargs, **{'table_id': table_id}})
            service=flask.current_app.config['SERVICE_NAME']
            try:
                dataset = dataset_from_service(service, table_id, service_token)
            except RuntimeError:
                resp = flask.Response("Invalid table_id for service", 400)
                return resp

            has_permission = required_permission in flask.g.auth_user.get('permissions_v2', {}).get(dataset, [])

            if not 'permissions_v2' in flask.g.auth_user: # backwards compatability
                required_level = ['none', 'view', 'edit'].index(required_permission)
                level_for_dataset = flask.g.auth_user.get('permissions', {}).get(dataset, 0)
                has_permission = level_for_dataset >= required_level

            if has_permission or flask.g.public_access(): # public_access won't be true for edit requests
                return f(*args, **{**kwargs, **{'table_id': table_id}})
            else:
                resp = flask.Response("Missing permission: {0} for dataset {1}".format(required_permission, dataset), 403)
                return resp

        return decorated_function
    return decorator

def auth_requires_group(required_group):
    def decorator(f):
        @wraps(f)
        @auth_required
        def decorated_function(*args, **kwargs):
            if flask.request.method == 'OPTIONS':
                return f(*args, **kwargs)

            if required_group not in flask.g.auth_user['groups']:
                resp = flask.Response("Requires membership of group: {0}".format(required_group), 403)
                return resp

            return f(*args, **kwargs)

        return decorated_function
    return decorator

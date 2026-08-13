import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BillingRejected(Exception):
    def __init__(self, message, status_code=400, data=None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data or {}


def _backend_url(path):
    base_url = os.getenv('WX_BACKEND_URL', '').rstrip('/')
    if not base_url:
        raise BillingRejected('转换计费服务未配置', 503)
    return f'{base_url}{path}'


def _json_request(path, payload, headers=None):
    request = Request(
        _backend_url(path),
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', **(headers or {})},
        method='POST',
    )
    try:
        with urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read().decode('utf-8') or '{}')
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode('utf-8') or '{}')
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        return error.code, body
    except (URLError, TimeoutError, OSError) as error:
        raise BillingRejected('转换计费服务暂时不可用，请稍后重试', 503) from error


def authorize_conversion(authorization, request_id, conversion_type, allow_charge=False):
    if not authorization:
        raise BillingRejected('请先登录后再使用转换功能', 401)
    if not request_id or len(request_id) > 64:
        raise BillingRejected('转换请求编号无效', 400)

    status, body = _json_request(
        '/api/v1/points/conversion/authorize',
        {
            'request_id': request_id,
            'conversion_type': conversion_type,
            'allow_charge': bool(allow_charge),
        },
        {'Authorization': authorization},
    )
    if status != 200:
        raise BillingRejected(body.get('message') or '转换额度确认失败', status, body.get('data'))
    data = body.get('data') or {}
    if not data.get('user_id'):
        raise BillingRejected('转换计费服务返回无效用户', 503)
    return data


def identify_user(authorization):
    if not authorization:
        raise BillingRejected('请先登录后再使用转换功能', 401)
    status, body = _json_request(
        '/api/v1/points/conversion/identity',
        {},
        {'Authorization': authorization},
    )
    if status != 200:
        raise BillingRejected(body.get('message') or '登录状态验证失败', status)
    user_id = (body.get('data') or {}).get('user_id')
    if not user_id:
        raise BillingRejected('登录状态验证失败', 401)
    return f'user:{user_id}'


def release_conversion(user_id, request_id, job_id=None):
    service_token = os.getenv('CONVERSION_SERVICE_TOKEN', '')
    if not service_token:
        return False
    try:
        status, _body = _json_request(
            '/api/v1/points/conversion/release',
            {'user_id': user_id, 'request_id': request_id, 'job_id': job_id},
            {'X-Conversion-Service-Token': service_token},
        )
        return status == 200
    except BillingRejected:
        return False


def commit_conversion(user_id, request_id, job_id):
    service_token = os.getenv('CONVERSION_SERVICE_TOKEN', '')
    if not service_token:
        raise BillingRejected('转换计费服务未配置', 503)
    status, body = _json_request(
        '/api/v1/points/conversion/commit',
        {'user_id': user_id, 'request_id': request_id, 'job_id': job_id},
        {'X-Conversion-Service-Token': service_token},
    )
    if status != 200:
        raise BillingRejected(body.get('message') or '转换额度提交失败', status, body.get('data'))
    return body.get('data') or {}

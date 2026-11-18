"""Qoder 活动领取（Windows）

凭据：%APPDATA%\\Qoder\\auth.v1.dat
  内容 = "v10" + DPAPI 加密的 JSON（Electron os_crypt on Windows）
  解密：跳过前 3 字节 "v10"，剩余二进制交给 CryptUnprotectData
  JSON 里递归找 access_token / accessToken，以及 user.id
  文件缺失或解密失败时退回到 Local Storage\\leveldb 里找 token（尽力而为）

接口（与 macOS 版实测一致）：
  GET  https://openapi.qoder.sh/sash/api/v1/me/campaigns
  POST https://openapi.qoder.sh/sash/api/v1/me/campaigns/{id}/claim

DPAPI 只在 Windows 可用：非 Windows 平台直接返回无账号，不抛异常。
"""
import glob
import json
import os
import re
import sys

import http_common

BASE_URL = "https://openapi.qoder.sh"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
USER_AGENT = "Qoder"
CLIENT_TYPE = "10"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_RE = re.compile(rb'"(?:access_?[Tt]oken)"\s*:\s*"([^"]{8,})"')

# 测试注入：覆盖真实凭据路径
auth_override = None


class QoderAuth:
    def __init__(self, token, uid=""):
        self.token = token
        self.uid = uid or ""

    def __repr__(self):
        return "QoderAuth(uid=%r)" % (self.uid,)


class Campaign:
    def __init__(self, campaign_id, title="", status=""):
        self.id = campaign_id
        self.title = title
        self.status = status or ""

    @property
    def claimable(self):
        return self.status.upper() == "CLAIMABLE"


class ClaimResult:
    def __init__(self, ok=False, message=""):
        self.ok = ok
        self.message = message


# ------------------------------------------------------------ 凭据

def auth_path():
    if auth_override is not None:
        return auth_override
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    return os.path.join(appdata, "Qoder", "auth.v1.dat")


def _dpapi_unprotect(payload):
    """Windows DPAPI 解密；非 Windows / 失败返回 None"""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DataBlob)]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        buffer = ctypes.create_string_buffer(payload, len(payload))
        blob_in = DataBlob(len(payload),
                           ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = DataBlob()
        if not crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    except Exception:
        return None


def load_auth(path=None):
    """读取并解密 Qoder 凭据；任何失败都返回 None（不抛异常）"""
    path = path or auth_path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return _load_from_leveldb(path)
    payload = raw[3:] if raw.startswith(b"v10") else raw
    plain = _dpapi_unprotect(payload)
    if plain:
        try:
            obj = json.loads(plain.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            obj = None
        auth = _parse_auth(obj)
        if auth:
            return auth
    return _load_from_leveldb(path)


def _parse_auth(obj):
    if not isinstance(obj, dict):
        return None
    token = _find_string(obj, ("access_token", "accessToken", "token"))
    if not token:
        return None
    user = _find_dict(obj, "user")
    uid = ""
    if isinstance(user, dict):
        uid = str(user.get("id") or user.get("uid") or "")
    return QoderAuth(token, uid)


def _load_from_leveldb(path):
    """弱兜底：在 Qoder 的 leveldb 里找 access_token（取最长的一条）"""
    root = os.path.dirname(path or auth_path())
    directory = os.path.join(root, "Local Storage", "leveldb")
    try:
        names = sorted(glob.glob(os.path.join(directory, "*")),
                       key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        return None
    best = ""
    for name in names[:20]:
        if not name.endswith((".ldb", ".log")):
            continue
        try:
            with open(name, "rb") as f:
                data = f.read(16 * 1024 * 1024)
        except OSError:
            continue
        for blob in _candidate_blobs(data):
            for match in _TOKEN_RE.finditer(blob):
                token = match.group(1).decode("utf-8", "ignore")
                if len(token) > len(best):
                    best = token
        if best:
            break
    return QoderAuth(best) if best else None


def _candidate_blobs(data):
    """leveldb 可能是 UTF-8 / UTF-16LE 存储，两种都试"""
    yield data
    try:
        yield data.decode("utf-16-le", "ignore").encode("utf-8")
    except Exception:
        return


# ------------------------------------------------------------ 请求

def headers(auth):
    return {
        "Authorization": "Bearer " + auth.token,
        "Cosy-ClientType": CLIENT_TYPE,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def campaigns(auth, get=None):
    """→ (campaigns, unauthorized)；网络/格式失败时 campaigns 为 None"""
    send = get or http_common.http_get
    try:
        http_status, _resp_headers, text = send(
            BASE_URL + CAMPAIGNS_PATH, headers=headers(auth), timeout=20)
    except Exception:
        return None, False
    if http_status == 401:
        return [], True
    if http_status == 204:
        return [], False
    if http_status != 200 or not text:
        return None, False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None, False
    found = []
    _collect(obj, found)
    seen = set()
    unique = []
    for item in found:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique, False


def _collect(node, out):
    if isinstance(node, list):
        for item in node:
            _collect(item, out)
        return
    if not isinstance(node, dict):
        return
    campaign_id = _text(node.get("campaignId")) or _text(node.get("campaign_id"))
    status = _text(node.get("claimStatus")) or _text(node.get("claim_status"))
    title = (_text(node.get("campaignKey")) or _text(node.get("campaign_key"))
             or _text(node.get("campaignName")) or _text(node.get("title")))
    if campaign_id and status:
        out.append(Campaign(campaign_id, title, status))
        return
    for value in node.values():
        _collect(value, out)


def claim(auth, campaign_id, post=None):
    if not campaign_id or not _ID_RE.match(campaign_id):
        return ClaimResult(False, "活动 id 非法")
    send = post or http_common.http_post
    url = "%s%s/%s/claim" % (BASE_URL, CAMPAIGNS_PATH, campaign_id)
    try:
        http_status, _resp_headers, text = send(
            url, headers=headers(auth), body="{}", timeout=20)
    except Exception:
        return ClaimResult(False, "网络请求失败")
    if http_status == 401:
        return ClaimResult(False, "登录已过期，请重新登录 Qoder")
    if http_status == 409:
        return ClaimResult(False, "活动已失效")
    if "CAMPAIGN_NOT_ACTIVE" in (text or ""):
        return ClaimResult(False, "活动已失效")
    try:
        obj = json.loads(text) if text else None
    except (ValueError, TypeError):
        obj = None
    if http_status == 200 and not _has_error(obj):
        return ClaimResult(True, "领取成功")
    message = _message(obj)
    return ClaimResult(False, message or "HTTP %s" % http_status)


def _has_error(obj):
    if not isinstance(obj, dict):
        return False
    error = obj.get("error")
    return bool(error)


def _message(obj):
    if not isinstance(obj, dict):
        return ""
    for key in ("message", "msg", "error", "errMsg"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ------------------------------------------------------------ 宽松取值

def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _find_string(node, names, depth=0):
    """递归找第一个非空字符串（键名大小写不敏感）"""
    if depth > 6:
        return ""
    if isinstance(node, dict):
        for name in names:
            for key, value in node.items():
                if str(key).lower() == name.lower() and isinstance(value, str) \
                        and value.strip():
                    return value.strip()
        for value in node.values():
            found = _find_string(value, names, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_string(item, names, depth + 1)
            if found:
                return found
    return ""


def _find_dict(node, key, depth=0):
    if depth > 6:
        return None
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, dict):
            return value
        for item in node.values():
            found = _find_dict(item, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_dict(item, key, depth + 1)
            if found is not None:
                return found
    return None

"""Real IM channel adapters (notify-only). Each implements the Channel protocol
from notify.py: normalize(declared binding) -> sendable target, and send_text(target,
text). Plain HTTPS/JSON, no SDK — the server always calls them through
asyncio.to_thread, never on the event loop.

Adding a channel: one class + one entry in build(). Interactive callbacks (action
cards, buttons) are a later milestone; these adapters only push text.

Deferred (not buildable notify-only without extra deps or a public callback URL):
dingtalk (Stream SDK), wecom (callback or websocket), discord (Gateway WS), teams.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 5  # a stuck IM call must not hold a notify thread (or a register) longer


def _http_json(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:200]}") from None


class TelegramChannel:
    """Bot-token DMs. id must be the numeric chat id — Telegram only lets a bot
    write into chats that messaged it first, so the owner opens the bot and /starts."""
    name = "telegram"

    def __init__(self, token):
        self.token = token

    def normalize(self, binding):
        cid = str(binding["id"]).strip()
        if not cid.lstrip("-").isdigit():
            raise ValueError("id must be the numeric chat id (open a chat with the bot and /start first)")
        return cid

    def send_text(self, target, text):
        r = _http_json("POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                       {"chat_id": target, "text": text})
        if not r.get("ok"):
            raise RuntimeError(f"telegram: {r.get('description')}")


class SlackChannel:
    """Bot-token DMs. Needs chat:write + im:write, plus users:read.email when
    owners declare their id as an email."""
    name = "slack"

    def __init__(self, token):
        self.token = token

    def normalize(self, binding):
        if binding["id_type"] == "member_id":
            return str(binding["id"]).strip()
        if binding["id_type"] == "email":
            r = _http_json("GET", "https://slack.com/api/users.lookupByEmail?email="
                           + urllib.parse.quote(str(binding["id"]).strip()),
                           headers={"Authorization": f"Bearer {self.token}"})
            if not r.get("ok"):
                raise RuntimeError(f"email lookup: {r.get('error')}")
            return r["user"]["id"]
        raise ValueError("id_type must be email or member_id")

    def send_text(self, target, text):
        r = _http_json("POST", "https://slack.com/api/chat.postMessage",
                       {"channel": target, "text": text},
                       headers={"Authorization": f"Bearer {self.token}"})
        if not r.get("ok"):
            raise RuntimeError(f"slack: {r.get('error')}")


class FeishuChannel:
    """企业自建应用 DMs (im:message + contact users/batch_get_id scopes).
    tenant_access_token is fetched on demand and cached (benign refresh race).
    Targets are prefixed with the receive_id_type so send_text knows how to address them."""
    name = "feishu"
    base = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret
        self._tok, self._tok_exp = "", 0.0

    def _token(self):
        if time.time() >= self._tok_exp - 60:
            r = _http_json("POST", f"{self.base}/auth/v3/tenant_access_token/internal",
                           {"app_id": self.app_id, "app_secret": self.app_secret})
            if r.get("code") != 0:
                raise RuntimeError(f"token: {r.get('msg')}")
            self._tok = r["tenant_access_token"]
            self._tok_exp = time.time() + r.get("expire", 7200)
        return self._tok

    def _lookup(self, email):
        # batch_get_id answers user_list[].user_id, typed by user_id_type (default open_id)
        r = _http_json("POST", f"{self.base}/contact/v3/users/batch_get_id?user_id_type=open_id",
                       {"emails": [email]},
                       headers={"Authorization": f"Bearer {self._token()}"})
        users = (r.get("data") or {}).get("user_list") or []
        uid = next((u["user_id"] for u in users if u.get("user_id")), None)
        if r.get("code") != 0 or not uid:
            raise RuntimeError(f"no open_id found for {email}")
        return uid

    def normalize(self, binding):
        t, v = binding["id_type"], str(binding["id"]).strip()
        if t in ("open_id", "chat_id"):
            return f"{t}:{v}"
        if t == "email":
            return f"open_id:{self._lookup(v)}"
        raise ValueError("id_type must be email | open_id | chat_id")

    def send_text(self, target, text):
        kind, uid = target.split(":", 1)
        r = _http_json("POST", f"{self.base}/im/v1/messages?receive_id_type={kind}",
                       {"receive_id": uid, "msg_type": "text", "content": json.dumps({"text": text})},
                       headers={"Authorization": f"Bearer {self._token()}"})
        if r.get("code") != 0:
            raise RuntimeError(f"send: {r.get('code')} {r.get('msg')}")


def build(cfg):
    """Channels whose credentials are present and complete; an absent credential
    simply leaves that channel unregistered (register answers channel_unavailable)."""
    out = {}
    f = cfg.get("feishu") or {}
    if f.get("app_id") and f.get("app_secret"):
        out["feishu"] = FeishuChannel(f["app_id"], f["app_secret"])
    if cfg.get("telegram_bot_token"):
        out["telegram"] = TelegramChannel(cfg["telegram_bot_token"])
    if cfg.get("slack_bot_token"):
        out["slack"] = SlackChannel(cfg["slack_bot_token"])
    return out

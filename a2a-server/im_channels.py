"""Real IM channel adapters (notify-only). Each implements the Channel protocol
from notify.py: normalize(declared binding) -> sendable target, and send_text(target,
text). Plain HTTPS/JSON, no SDK — the server always calls them through
asyncio.to_thread, never on the event loop.

Adding a channel: one class + one entry in build(). Interactive callbacks (action
cards, buttons) are a later milestone; these adapters only push text.

Deferred: teams / whatsapp (need a public callback or cloud API). DingTalk sends
are asynchronous work-notifications (errcode 0 = accepted for delivery).
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


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clip_bytes(text, limit):
    raw = text.encode()
    return text if len(raw) <= limit else raw[:limit - 3].decode(errors="ignore") + "…"


class _TokenClient:
    """Cached platform access token; benign race if two threads refresh together."""

    def __init__(self, fetch):
        self._fetch = fetch  # () -> (token, ttl_seconds); raises on failure
        self._tok, self._exp = "", 0.0

    def get(self):
        if time.time() >= self._exp - 60:
            self._tok, ttl = self._fetch()
            self._exp = time.time() + ttl
        return self._tok


class TelegramChannel:
    """Bot-token DMs. id must be the numeric chat id — Telegram only lets a bot
    write into chats that messaged it first, so the owner opens the bot and /starts."""

    def __init__(self, token):
        self.token = token

    def normalize(self, binding):
        cid = str(binding["id"]).strip()
        if not cid.lstrip("-").isdigit():
            raise ValueError("id must be the numeric chat id (open a chat with the bot and /start first)")
        return cid

    def send_text(self, target, text):
        r = _http_json("POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                       {"chat_id": target, "text": _clip(text, 4000)})
        if not r.get("ok"):
            raise RuntimeError(f"telegram: {r.get('description')}")


class SlackChannel:
    """Bot-token DMs. Needs chat:write + im:write, plus users:read.email when
    owners declare their id as an email."""

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
    Targets are prefixed with the receive_id_type so send_text knows how to address them."""
    base = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret

        def fetch():
            r = _http_json("POST", f"{self.base}/auth/v3/tenant_access_token/internal",
                           {"app_id": self.app_id, "app_secret": self.app_secret})
            if r.get("code") != 0:
                raise RuntimeError(f"token: {r.get('msg')}")
            return r["tenant_access_token"], r.get("expire", 7200)

        self._tokens = _TokenClient(fetch)

    def _lookup(self, email):
        # batch_get_id answers user_list[].user_id, typed by user_id_type (default open_id)
        r = _http_json("POST", f"{self.base}/contact/v3/users/batch_get_id?user_id_type=open_id",
                       {"emails": [email]},
                       headers={"Authorization": f"Bearer {self._tokens.get()}"})
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
                       headers={"Authorization": f"Bearer {self._tokens.get()}"})
        if r.get("code") != 0:
            raise RuntimeError(f"send: {r.get('code')} {r.get('msg')}")


class DingTalkChannel:
    """企业内部应用工作通知 (appKey/appSecret/agentId). Lookup is by mobile — the
    number must exist in the org directory (a >20% lookup error rate blocks the
    API for a day, so owners must type it correctly)."""
    base = "https://oapi.dingtalk.com"

    def __init__(self, app_key, app_secret, agent_id):
        self.app_key, self.app_secret, self.agent_id = app_key, app_secret, int(agent_id)

        def fetch():
            r = _http_json("GET", f"{self.base}/gettoken?appkey={urllib.parse.quote(self.app_key)}"
                           f"&appsecret={urllib.parse.quote(self.app_secret)}")
            if r.get("errcode") != 0:
                raise RuntimeError(f"token: {r.get('errmsg')}")
            return r["access_token"], r.get("expires_in", 7200)

        self._tokens = _TokenClient(fetch)

    def normalize(self, binding):
        t, v = binding["id_type"], str(binding["id"]).strip()
        if t == "userid":
            return v
        if t == "mobile":
            r = _http_json("POST", f"{self.base}/topapi/v2/user/getbymobile?access_token={self._tokens.get()}",
                           {"mobile": v})
            if r.get("errcode") != 0 or not (r.get("result") or {}).get("userid"):
                raise RuntimeError(f"mobile lookup: {r.get('errmsg') or 'no userid'}")
            return r["result"]["userid"]
        raise ValueError("id_type must be mobile or userid")

    def send_text(self, target, text):
        r = _http_json("POST", f"{self.base}/topapi/message/corpconversation/asyncsend_v2"
                       f"?access_token={self._tokens.get()}",
                       {"agent_id": self.agent_id, "userid_list": target,
                        "msg": {"msgtype": "text", "text": {"content": _clip_bytes(text, 500)}}})
        if r.get("errcode") != 0:
            raise RuntimeError(f"send: {r.get('errcode')} {r.get('errmsg')}")


class WeComChannel:
    """企业微信自建应用消息 (corpId/corpSecret/agentId). Text is capped at 2048
    bytes by the platform; longer texts are byte-clipped. Recipients must be in
    the app's visible range and the server's egress IP in its trusted-IP list."""
    base = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, corp_id, corp_secret, agent_id):
        self.corp_id, self.corp_secret, self.agent_id = corp_id, corp_secret, int(agent_id)

        def fetch():
            r = _http_json("GET", f"{self.base}/gettoken?corpid={urllib.parse.quote(self.corp_id)}"
                           f"&corpsecret={urllib.parse.quote(self.corp_secret)}")
            if r.get("errcode") != 0:
                raise RuntimeError(f"token: {r.get('errmsg')}")
            return r["access_token"], r.get("expires_in", 7200)

        self._tokens = _TokenClient(fetch)

    def normalize(self, binding):
        t, v = binding["id_type"], str(binding["id"]).strip()
        if t == "userid":
            return v
        if t == "mobile":
            r = _http_json("POST", f"{self.base}/user/getuserid?access_token={self._tokens.get()}",
                           {"mobile": v})
            if r.get("errcode") != 0 or not r.get("userid"):
                raise RuntimeError(f"mobile lookup: {r.get('errmsg') or 'no userid'}")
            return r["userid"]
        raise ValueError("id_type must be mobile or userid")

    def send_text(self, target, text):
        r = _http_json("POST", f"{self.base}/message/send?access_token={self._tokens.get()}",
                       {"touser": target, "msgtype": "text", "agentid": self.agent_id,
                        "text": {"content": _clip_bytes(text, 2048)}})
        if r.get("errcode") != 0:
            raise RuntimeError(f"send: {r.get('errcode')} {r.get('errmsg')}")


class DiscordChannel:
    """Bot-token DMs. The owner's id is their snowflake user id (Discord: settings
    -> advanced -> developer mode -> copy user id). The DM channel is opened once
    per recipient and cached; a rate-limit (429) surfaces as notify_failed."""
    base = "https://discord.com/api/v10"

    def __init__(self, token):
        self.token = token
        self._dm = {}  # user id -> dm channel id

    def normalize(self, binding):
        uid = str(binding["id"]).strip()
        if not uid.isdigit():
            raise ValueError("id must be the numeric user id (snowflake)")
        return uid

    def send_text(self, target, text):
        headers = {"Authorization": f"Bot {self.token}"}
        if target not in self._dm:
            r = _http_json("POST", f"{self.base}/users/@me/channels",
                           {"recipient_id": target}, headers=headers)
            self._dm[target] = r["id"]
        _http_json("POST", f"{self.base}/channels/{self._dm[target]}/messages",
                   {"content": _clip(text, 2000)}, headers=headers)


def build(cfg):
    """Channels whose credentials are present and complete; an absent credential
    simply leaves that channel unregistered (register answers channel_unavailable)."""
    out = {}
    for key, cls, fields in (("feishu", FeishuChannel, ("app_id", "app_secret")),
                             ("dingtalk", DingTalkChannel, ("app_key", "app_secret", "agent_id")),
                             ("wecom", WeComChannel, ("corp_id", "corp_secret", "agent_id"))):
        c = cfg.get(key) or {}
        if all(c.get(f) for f in fields):
            try:
                out[key] = cls(*(c[f] for f in fields))
            except (TypeError, ValueError) as e:
                raise SystemExit(f"config error: {key}: {e}") from None
    if cfg.get("telegram_bot_token"):
        out["telegram"] = TelegramChannel(cfg["telegram_bot_token"])
    if cfg.get("slack_bot_token"):
        out["slack"] = SlackChannel(cfg["slack_bot_token"])
    if cfg.get("discord_bot_token"):
        out["discord"] = DiscordChannel(cfg["discord_bot_token"])
    return out

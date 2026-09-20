"""Notification gateway: i18n message catalog + channel registry.

A channel only needs normalize()/send_text(); IM adapters (feishu/dingtalk/slack/...)
register into CHANNELS without touching business code.
"""

MESSAGES = {
    "zh": {
        "register_verify": "A2A 接入验证：{owner} 你好，agent「{agent}」注册成功，本条消息用于确认通知通道畅通。",
        "task_received": "{initiator} 给你的 agent「{agent}」派了任务：{summary}\n已同时开始执行；如需中止：a2a-skill/api.py action --task {task_id} --action abort",
        "manual_confirm": "{initiator} 给你的 agent「{agent}」派了任务，等你确认：{summary}\n接受：a2a-skill/api.py action --task {task_id} --action approve\n拒绝：--action reject",
        "manual_dispatch": "任务已交付（manual 模式，由你代跑）：{summary}\n完成后回填：a2a-worker report {task_id} --status completed --output \"...\"\n中止：a2a-skill/api.py action --task {task_id} --action abort",
        "task_completed": "任务完成：{initiator} → {target}\n{output_head}",
        "task_failed": "任务失败（{reason}）：{initiator} → {target}\n详情 GET {base}/domains/{domain}/tasks/{task_id}",
        "task_canceled": "任务已取消（{reason}）：{initiator} → {target}",
        "late_result": "收到迟到结果：任务 {task_id} 已处于终态，结果未采纳，请人工裁决。",
        "input_required": "{target} 就任务 {task_id} 追问：{question}\n续发：POST {base}/domains/{domain}/tasks/{task_id}/messages",
        "agent_cleaned": "agent「{agent}」已离线超过 {days} 天，已从目录移除。重新上线注册即可恢复。",
        "agent_deregistered": "agent「{agent}」已注销，其未完成任务已置为失败。",
    },
    "en": {
        "register_verify": "A2A onboarding check: hi {owner}, agent \"{agent}\" is registered. This message confirms your notification channel works.",
        "task_received": "{initiator} sent a task to your agent \"{agent}\": {summary}\nIt is already running. To abort: a2a-skill/api.py action --task {task_id} --action abort",
        "manual_confirm": "{initiator} sent a task to your agent \"{agent}\" and it is waiting for your approval: {summary}\nApprove: a2a-skill/api.py action --task {task_id} --action approve\nReject: --action reject",
        "manual_dispatch": "Task delivered (manual mode, you run it): {summary}\nThen fill in the result: a2a-worker report {task_id} --status completed --output \"...\"\nAbort: a2a-skill/api.py action --task {task_id} --action abort",
        "task_completed": "Task completed: {initiator} → {target}\n{output_head}",
        "task_failed": "Task failed ({reason}): {initiator} → {target}\nDetails: GET {base}/domains/{domain}/tasks/{task_id}",
        "task_canceled": "Task canceled ({reason}): {initiator} → {target}",
        "late_result": "Late result received: task {task_id} is already in a terminal state; the result was not applied. Please review manually.",
        "input_required": "{target} asks about task {task_id}: {question}\nReply: POST {base}/domains/{domain}/tasks/{task_id}/messages",
        "agent_cleaned": "Agent \"{agent}\" has been offline for more than {days} days and was removed from the directory. Register again to rejoin.",
        "agent_deregistered": "Agent \"{agent}\" was deregistered; its unfinished tasks were marked as failed.",
    },
}

REASONS = {
    "zh": {
        "source_not_allowed": "来源不在白名单", "rejected": "属主拒绝", "approval_expired": "确认超时",
        "worker_offline": "执行方离线", "worker_restart": "执行方重启", "receive_timeout": "领取后未启动",
        "timeout": "执行超时", "timeout_stale": "执行超时（服务端兜底）", "driver_error": "driver 执行失败",
        "manual_expired": "人工回填超时", "input_timeout": "追问未响应", "agent_deregistered": "agent 已注销",
        "canceled_by_initiator": "发起方取消", "canceled_by_owner": "属主中止",
    },
    "en": {
        "source_not_allowed": "source not allowed", "rejected": "rejected by owner", "approval_expired": "approval timed out",
        "worker_offline": "worker offline", "worker_restart": "worker restarted", "receive_timeout": "never started after dispatch",
        "timeout": "execution timeout", "timeout_stale": "execution timeout (server fallback)", "driver_error": "driver error",
        "manual_expired": "manual report timed out", "input_timeout": "no reply to follow-up question", "agent_deregistered": "agent deregistered",
        "canceled_by_initiator": "canceled by initiator", "canceled_by_owner": "aborted by owner",
    },
}


_LANG = "zh"


def set_lang(lang):
    global _LANG
    _LANG = lang  # server.py validates against MESSAGES at startup; no second gate here


def t(key, **ctx):
    return MESSAGES[_LANG][key].format(**ctx)


def reason_desc(reason):
    return REASONS[_LANG].get(reason, reason)


class LogChannel:
    """Dev channel: notifications go to the server log. No external IM needed."""
    name = "log"

    def normalize(self, binding):
        return binding["id"]

    def send_text(self, target, text):
        print(f"[notify:{self.name}] -> {target}: {text}", flush=True)


CHANNELS = {"log": LogChannel()}

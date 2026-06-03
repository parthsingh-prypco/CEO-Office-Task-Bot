"""
CEO Office Slack Bot
====================
Architecture:
  - Slack Workflow adds tasks to the Slack List (native) AND calls /webhook/add-task
  - Bot stores tasks in Upstash Redis for querying
  - Slash commands + cron digests read from Redis

Endpoints:
  POST /slack/events          — slash commands
  POST /webhook/add-task      — called by Slack Workflow when a task is added
  GET  /cron/due-today        — 9am daily digest (via cron-job.org)
  GET  /cron/check-changes    — change detection (via cron-job.org)
  GET  /cron/weekly-digest    — Monday summary (via cron-job.org)
"""

import os
import json
import uuid
import logging
from datetime import datetime, timedelta

from flask import Flask, request, make_response
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
NOTIFY_CHANNEL       = os.environ.get("NOTIFY_CHANNEL", "#ceo-office")
CRON_SECRET          = os.environ.get("CRON_SECRET", "")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")   # set same value in Slack Workflow header

# ─── App setup ────────────────────────────────────────────────────────────────

bolt_app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET,
    process_before_response=True,
)
flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

# ─── Redis task store ─────────────────────────────────────────────────────────

def get_tasks() -> list:
    raw = redis.get("tasks")
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


def save_tasks(tasks: list):
    redis.set("tasks", json.dumps(tasks))


def add_task(data: dict) -> dict:
    tasks = get_tasks()
    task = {
        "id":          str(uuid.uuid4()),
        "title":       data.get("title", "Untitled"),
        "due_date":    data.get("due_date", ""),
        "assignee":    data.get("assignee", ""),
        "priority":    data.get("priority", ""),
        "project":     data.get("project", ""),
        "description": data.get("description", ""),
        "status":      data.get("status", "Not Started"),
        "created_at":  datetime.now().isoformat(),
    }
    tasks.append(task)
    save_tasks(tasks)
    return task


def update_task_by_id(task_id: str, updates: dict) -> bool:
    tasks = get_tasks()
    for i, t in enumerate(tasks):
        if t["id"] == task_id:
            tasks[i].update(updates)
            save_tasks(tasks)
            return True
    return False


def find_task_by_name(name: str) -> dict | None:
    return next(
        (t for t in get_tasks() if name.lower() in t.get("title", "").lower()),
        None,
    )


def get_due_today(tasks=None) -> list:
    if tasks is None:
        tasks = get_tasks()
    today = str(datetime.now().date())
    return [
        t for t in tasks
        if t.get("due_date", "")[:10] == today and t.get("status", "").lower() != "done"
    ]


def get_due_this_week_tasks(tasks=None) -> list:
    if tasks is None:
        tasks = get_tasks()
    today       = datetime.now().date()
    end_of_week = today + timedelta(days=(6 - today.weekday()))
    result = []
    for t in tasks:
        raw = t.get("due_date", "")
        if not raw:
            continue
        try:
            d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
            if today <= d <= end_of_week and t.get("status", "").lower() != "done":
                result.append((t, d))
        except ValueError:
            continue
    result.sort(key=lambda x: x[1])
    return [x[0] for x in result]


def get_overdue(tasks=None) -> list:
    if tasks is None:
        tasks = get_tasks()
    today = datetime.now().date()
    result = []
    for t in tasks:
        raw = t.get("due_date", "")
        if not raw or t.get("status", "").lower() == "done":
            continue
        try:
            if datetime.strptime(str(raw)[:10], "%Y-%m-%d").date() < today:
                result.append(t)
        except ValueError:
            continue
    return result


def format_task(t: dict) -> str:
    title    = t.get("title", "Untitled")
    status   = t.get("status", "Not Started")
    assignee = t.get("assignee", "—")
    due      = t.get("due_date", "—") or "—"
    priority = t.get("priority", "")
    project  = t.get("project", "")
    tags     = " ".join(f"`{x}`" for x in [priority, project] if x)
    return f"• *{title}* {tags}\n  Status: {status} | Due: {due} | Assignee: {assignee}"

# ─── Slash commands ───────────────────────────────────────────────────────────

@bolt_app.command("/add-task")
def handle_add_task(ack, respond, command):
    """
    /add-task Task name | YYYY-MM-DD | assignee | Priority | Project
    Tip: Use the Slack Workflow link for a nicer form experience.
    """
    ack()
    text = command.get("text", "").strip()
    if not text:
        respond("Usage: `/add-task Task name | YYYY-MM-DD | assignee | Priority | Project`")
        return

    parts    = [p.strip() for p in text.split("|")]
    task = add_task({
        "title":    parts[0],
        "due_date": parts[1] if len(parts) > 1 else "",
        "assignee": parts[2] if len(parts) > 2 else "",
        "priority": parts[3] if len(parts) > 3 else "",
        "project":  parts[4] if len(parts) > 4 else "",
    })
    respond(f"✅ Added *{task['title']}* to the Project Tracker!")


@bolt_app.command("/due-this-week")
def handle_due_this_week(ack, respond):
    ack()
    items = get_due_this_week_tasks()
    if not items:
        respond("🎉 No tasks due this week!")
        return
    lines = [f"*📅 Due this week — {len(items)} task(s):*\n"] + [format_task(t) for t in items]
    respond("\n".join(lines))


@bolt_app.command("/task-list")
def handle_task_list(ack, respond, command):
    """
    /task-list                   — all tasks
    /task-list status:In Progress
    /task-list project:Commercial
    /task-list priority:High
    """
    ack()
    text  = command.get("text", "").strip()
    tasks = get_tasks()

    if text:
        key, _, val = text.partition(":")
        key = key.strip().lower()
        val = val.strip().lower()
        field_map = {"status": "status", "project": "project", "priority": "priority", "assignee": "assignee"}
        if key in field_map:
            tasks = [t for t in tasks if str(t.get(field_map[key], "")).lower() == val]

    if not tasks:
        respond("No tasks found.")
        return
    lines = [f"*✅ Project Tracker — {len(tasks)} task(s):*\n"] + [format_task(t) for t in tasks]
    respond("\n".join(lines))


@bolt_app.command("/update-task")
def handle_update_task(ack, respond, command):
    """
    /update-task Task name | field | new value
    Fields: status, priority, due_date, project, assignee
    Example: /update-task Review Q2 | status | Done
    """
    ack()
    parts = [p.strip() for p in command.get("text", "").split("|")]
    if len(parts) < 3:
        respond("Usage: `/update-task Task name | field | new value`\nFields: `status`, `priority`, `due_date`, `project`, `assignee`")
        return

    search, field, new_value = parts[0], parts[1].lower(), parts[2]
    task = find_task_by_name(search)
    if not task:
        respond(f"❌ No task found matching *{search}*")
        return

    field_map = {"status": "status", "priority": "priority", "due_date": "due_date", "due": "due_date", "project": "project", "assignee": "assignee"}
    key = field_map.get(field, field)
    update_task_by_id(task["id"], {key: new_value})
    respond(f"✅ Updated *{task['title']}*: {key} → `{new_value}`")

# ─── Webhook (called by Slack Workflow after adding to list) ──────────────────

@flask_app.route("/webhook/add-task", methods=["POST"])
def webhook_add_task():
    """
    Slack Workflow calls this after adding a task to the list.
    Configure the workflow's webhook step with:
      Header: X-Webhook-Secret = your WEBHOOK_SECRET value
      Body (JSON):
        {
          "title":       "{{Task name}}",
          "due_date":    "{{Due date}}",
          "assignee":    "{{Assignee}}",
          "priority":    "{{Priority}}",
          "project":     "{{Project}}",
          "description": "{{Description}}"
        }
    """
    secret = request.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return make_response("Unauthorized", 401)

    data = request.get_json(silent=True) or {}
    if not data.get("title"):
        return make_response({"ok": False, "error": "title required"}, 400)

    task = add_task(data)
    logger.info(f"Task added via webhook: {task['title']}")
    return {"ok": True, "task_id": task["id"]}

# ─── Cron endpoints ───────────────────────────────────────────────────────────

def _auth_cron(req) -> bool:
    auth = req.headers.get("Authorization", "")
    return not CRON_SECRET or auth == f"Bearer {CRON_SECRET}"


@flask_app.route("/cron/due-today", methods=["GET"])
def cron_due_today():
    """Daily 9am digest — tasks due today."""
    if not _auth_cron(request):
        return make_response("Unauthorized", 401)

    tasks    = get_tasks()
    today    = get_due_today(tasks)
    overdue  = get_overdue(tasks)
    date_str = datetime.now().strftime("%A, %B %d")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📅 Good morning — {date_str}"}},
        {"type": "divider"},
    ]

    if overdue:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🔴 *Overdue ({len(overdue)}):*\n" + "\n".join(format_task(t) for t in overdue)}})

    if today:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"📌 *Due Today ({len(today)}):*\n" + "\n".join(format_task(t) for t in today)}})

    if not overdue and not today:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "✅ Nothing due today. Have a great day!"}})

    bolt_app.client.chat_postMessage(
        channel=NOTIFY_CHANNEL,
        text=f"Daily digest — {date_str}",
        blocks=blocks,
    )
    return {"ok": True, "due_today": len(today), "overdue": len(overdue)}


@flask_app.route("/cron/check-changes", methods=["GET"])
def cron_check_changes():
    """Every 5 min — detect changes vs last snapshot and notify."""
    if not _auth_cron(request):
        return make_response("Unauthorized", 401)

    current   = {t["id"]: t for t in get_tasks()}
    prev_json = redis.get("tasks_snapshot") or "{}"
    previous  = json.loads(prev_json) if isinstance(prev_json, str) else prev_json
    changes   = []

    for tid, task in current.items():
        title = task.get("title", "Untitled")
        if tid not in previous:
            changes.append(f"➕ *New task:* {title}")
        else:
            for col in ["status", "due_date", "assignee", "priority"]:
                if previous[tid].get(col) != task.get(col):
                    changes.append(f"✏️ *{title}* — {col}: `{previous[tid].get(col, '—')}` → `{task.get(col, '—')}`")

    for tid, task in previous.items():
        if tid not in current:
            changes.append(f"🗑️ *Removed:* {task.get('title', 'Untitled')}")

    if changes:
        bolt_app.client.chat_postMessage(
            channel=NOTIFY_CHANNEL,
            text="*📋 Project Tracker updated:*\n" + "\n".join(changes),
        )

    redis.set("tasks_snapshot", json.dumps(current))
    return {"ok": True, "changes_detected": len(changes)}


@flask_app.route("/cron/weekly-digest", methods=["GET"])
def cron_weekly_digest():
    """Monday 9am — weekly summary."""
    if not _auth_cron(request):
        return make_response("Unauthorized", 401)

    tasks     = get_tasks()
    due_week  = get_due_this_week_tasks(tasks)
    overdue   = get_overdue(tasks)
    today     = datetime.now().date()

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📋 CEO Office — Weekly Digest"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Week of {today.strftime('%B %d, %Y')}*"}},
        {"type": "divider"},
    ]
    if overdue:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🔴 *Overdue ({len(overdue)}):*\n" + "\n".join(format_task(t) for t in overdue)}})
    if due_week:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"📅 *Due This Week ({len(due_week)}):*\n" + "\n".join(format_task(t) for t in due_week)}})
    if not overdue and not due_week:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "✅ Clean slate this week!"}})

    bolt_app.client.chat_postMessage(
        channel=NOTIFY_CHANNEL,
        text="CEO Office Weekly Digest",
        blocks=blocks,
    )
    return {"ok": True, "overdue": len(overdue), "due_this_week": len(due_week)}


@bolt_app.command("/due-today")
def handle_due_today(ack, respond):
    ack()
    tasks   = get_tasks()
    today   = get_due_today(tasks)
    overdue = get_overdue(tasks)
    date_str = datetime.now().strftime("%A, %B %d")
    lines = [f"*📅 {date_str}*
"]
    if overdue:
        lines.append(f"🔴 *Overdue ({len(overdue)}):*")
        lines += [format_task(t) for t in overdue]
    if today:
        lines.append(f"
📌 *Due Today ({len(today)}):*")
        lines += [format_task(t) for t in today]
    if not overdue and not today:
        lines.append("✅ Nothing due today. Have a great day!")
    respond("
".join(lines))


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


# Vercel WSGI entry point
app = flask_app

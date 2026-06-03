"""
CEO Office Slack Bot
====================
Handles slash commands and cron jobs for the Project Tracker list.

Slash commands:
  /add-task     Task name | YYYY-MM-DD | @owner | Priority | Track
  /due-this-week
  /task-list    [status:In Progress]
  /update-task  Task name | field | new value

Cron endpoints (called by Vercel cron):
  GET /cron/check-changes   — every 5 min, posts diffs to Slack
  GET /cron/weekly-digest   — Monday 9am GST, posts weekly summary
"""

import os
import json
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
LIST_ID              = os.environ.get("PROJECT_LIST_ID", "F0B7RB480DA")
NOTIFY_CHANNEL       = os.environ.get("NOTIFY_CHANNEL", "#ceo-office")
CRON_SECRET          = os.environ.get("CRON_SECRET", "")

# ─── App setup ────────────────────────────────────────────────────────────────

bolt_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET, process_before_response=True)
flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

# ─── Column names ─────────────────────────────────────────────────────────────
# These must match your list's column names exactly (check in Slack).

COL_TITLE    = "Title"
COL_STATUS   = "Status"     # e.g. "Not Started", "In Progress", "Done"
COL_DUE      = "Due date"
COL_OWNER    = "Assignee"
COL_PRIORITY = "Priority"   # e.g. "High", "Medium", "Low"
COL_TRACK    = "Project"      # e.g. "Commercial", "Strategy"

# ─── Lists API helpers ────────────────────────────────────────────────────────

def get_list_items() -> list:
    resp = bolt_app.client.api_call("lists.items.list", json={"list_id": LIST_ID})
    if not resp.get("ok"):
        logger.error(f"lists.items.list failed: {resp.get('error')}")
        return []
    return resp.get("items", [])


def create_list_item(task, due_date=None, owner_id=None, priority=None, track=None):
    columns = {COL_TITLE: task, COL_STATUS: "Not Started"}
    if due_date:  columns[COL_DUE]      = due_date
    if owner_id:  columns[COL_OWNER]    = owner_id
    if priority:  columns[COL_PRIORITY] = priority
    if track:     columns[COL_TRACK]    = track
    resp = bolt_app.client.api_call("lists.items.create", json={"list_id": LIST_ID, "columns": columns})
    return resp.get("ok", False), resp


def update_list_item(item_id, columns):
    resp = bolt_app.client.api_call("lists.items.update", json={"list_id": LIST_ID, "item_id": item_id, "columns": columns})
    return resp.get("ok", False), resp


def get_due_this_week(items=None) -> list:
    if items is None:
        items = get_list_items()
    today       = datetime.now().date()
    end_of_week = today + timedelta(days=(6 - today.weekday()))
    due = []
    for item in items:
        raw = item.get("columns", {}).get(COL_DUE, "")
        if not raw:
            continue
        try:
            d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
            if today <= d <= end_of_week:
                due.append((item, d))
        except ValueError:
            continue
    due.sort(key=lambda x: x[1])
    return [x[0] for x in due]


def format_item(item) -> str:
    cols     = item.get("columns", {})
    title    = cols.get(COL_TITLE, "Untitled")
    status   = cols.get(COL_STATUS, "—")
    owner    = cols.get(COL_OWNER, "—")
    due      = cols.get(COL_DUE, "—")
    priority = cols.get(COL_PRIORITY, "")
    track    = cols.get(COL_TRACK, "")
    tags     = " ".join(f"`{t}`" for t in [priority, track] if t)
    return f"• *{title}* {tags}\n  Status: {status} | Due: {due} | Owner: {owner}"


def resolve_user(raw, client):
    raw = raw.lstrip("@").strip()
    if not raw:
        return None
    try:
        return client.users_lookupByEmail(email=raw)["user"]["id"]
    except Exception:
        pass
    try:
        for u in client.users_list()["members"]:
            if u.get("name") == raw or u.get("real_name", "").lower() == raw.lower():
                return u["id"]
    except Exception:
        pass
    return None

# ─── Slash commands ───────────────────────────────────────────────────────────

@bolt_app.command("/add-task")
def handle_add_task(ack, respond, command, client):
    """
    /add-task Task name | YYYY-MM-DD | @owner | Priority | Track
    Example: /add-task Review Q2 report | 2026-06-10 | @parth | High | Commercial
    """
    ack()
    text = command.get("text", "").strip()
    if not text:
        respond("Usage: `/add-task Task name | YYYY-MM-DD | @owner | Priority | Track`\nAll fields after the task name are optional.")
        return

    parts     = [p.strip() for p in text.split("|")]
    task_name = parts[0]
    due_date  = parts[1] if len(parts) > 1 else None
    owner_raw = parts[2] if len(parts) > 2 else None
    priority  = parts[3] if len(parts) > 3 else None
    track     = parts[4] if len(parts) > 4 else None
    owner_id  = resolve_user(owner_raw, client) if owner_raw else None

    ok, resp = create_list_item(task_name, due_date, owner_id, priority, track)
    if ok:
        details = [x for x in [due_date and f"Due: {due_date}", owner_raw and f"Owner: {owner_raw.lstrip('@')}", priority and f"Priority: {priority}", track and f"Track: {track}"] if x]
        suffix  = f" ({' · '.join(details)})" if details else ""
        respond(f"✅ Added *{task_name}*{suffix} to the Project Tracker!")
    else:
        respond(f"❌ Failed to add task: `{resp.get('error', 'unknown error')}`")


@bolt_app.command("/due-this-week")
def handle_due_this_week(ack, respond):
    ack()
    items = get_due_this_week()
    if not items:
        respond("🎉 No tasks due this week!")
        return
    lines = [f"*📅 Due this week — {len(items)} task(s):*\n"] + [format_item(i) for i in items]
    respond("\n".join(lines))


@bolt_app.command("/task-list")
def handle_task_list(ack, respond, command):
    """
    /task-list                    — all tasks
    /task-list status:In Progress
    /task-list track:Commercial
    /task-list priority:High
    """
    ack()
    text  = command.get("text", "").strip()
    items = get_list_items()

    if text:
        key, _, val = text.partition(":")
        col_map = {"status": COL_STATUS, "track": COL_TRACK, "priority": COL_PRIORITY, "owner": COL_OWNER}
        col = col_map.get(key.strip().lower())
        if col:
            items = [i for i in items if str(i.get("columns", {}).get(col, "")).lower() == val.strip().lower()]

    if not items:
        respond("No tasks found.")
        return
    lines = [f"*✅ Project Tracker — {len(items)} task(s):*\n"] + [format_item(i) for i in items]
    respond("\n".join(lines))


@bolt_app.command("/update-task")
def handle_update_task(ack, respond, command):
    """
    /update-task Task name | field | new value
    Fields: status, priority, due_date, track
    Example: /update-task Review Q2 | status | Done
    """
    ack()
    parts = [p.strip() for p in command.get("text", "").split("|")]
    if len(parts) < 3:
        respond("Usage: `/update-task Task name | field | new value`\nFields: `status`, `priority`, `due_date`, `track`")
        return

    search, field, new_value = parts[0], parts[1].lower(), parts[2]
    items  = get_list_items()
    target = next((i for i in items if search.lower() in i.get("columns", {}).get(COL_TITLE, "").lower()), None)
    if not target:
        respond(f"❌ No task found matching *{search}*")
        return

    col = {"status": COL_STATUS, "priority": COL_PRIORITY, "due_date": COL_DUE, "due": COL_DUE, "track": COL_TRACK}.get(field, field)
    ok, resp = update_list_item(target["id"], {col: new_value})
    title = target["columns"].get(COL_TITLE, "task")
    if ok:
        respond(f"✅ Updated *{title}*: {col} → `{new_value}`")
    else:
        respond(f"❌ Update failed: `{resp.get('error', 'unknown')}`")

# ─── Change detection ─────────────────────────────────────────────────────────

def check_for_changes() -> list:
    current   = {i["id"]: i for i in get_list_items()}
    prev_json = redis.get("list_snapshot") or "{}"
    previous  = json.loads(prev_json)
    changes   = []

    for iid, item in current.items():
        title = item.get("columns", {}).get(COL_TITLE, "Untitled")
        if iid not in previous:
            changes.append(f"➕ *New task:* {title}")
        else:
            prev_cols = previous[iid].get("columns", {})
            for col, val in item.get("columns", {}).items():
                if prev_cols.get(col) != val:
                    changes.append(f"✏️ *{title}* — {col}: `{prev_cols.get(col, '—')}` → `{val}`")

    for iid, item in previous.items():
        if iid not in current:
            changes.append(f"🗑️ *Removed:* {item.get('columns', {}).get(COL_TITLE, 'Untitled')}")

    if changes:
        bolt_app.client.chat_postMessage(
            channel=NOTIFY_CHANNEL,
            text="*📋 Project Tracker updated:*\n" + "\n".join(changes),
        )

    redis.set("list_snapshot", json.dumps(current))
    return changes

# ─── Routes ───────────────────────────────────────────────────────────────────

def _auth_cron(req) -> bool:
    auth = req.headers.get("Authorization", "")
    return not CRON_SECRET or auth == f"Bearer {CRON_SECRET}"


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/cron/check-changes", methods=["GET"])
def cron_check_changes():
    if not _auth_cron(request):
        return make_response("Unauthorized", 401)
    changes = check_for_changes()
    return {"ok": True, "changes_detected": len(changes)}


@flask_app.route("/cron/weekly-digest", methods=["GET"])
def cron_weekly_digest():
    if not _auth_cron(request):
        return make_response("Unauthorized", 401)

    today = datetime.now().date()
    items = get_list_items()
    due_items = get_due_this_week(items)
    overdue = []

    for item in items:
        raw    = item.get("columns", {}).get(COL_DUE, "")
        status = item.get("columns", {}).get(COL_STATUS, "")
        if not raw or status.lower() == "done":
            continue
        try:
            if datetime.strptime(str(raw)[:10], "%Y-%m-%d").date() < today:
                overdue.append(item)
        except ValueError:
            continue

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📋 CEO Office — Weekly Task Digest"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Week of {today.strftime('%B %d, %Y')}*"}},
        {"type": "divider"},
    ]
    if overdue:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🔴 *Overdue ({len(overdue)}):*\n" + "\n".join(format_item(i) for i in overdue)}})
    if due_items:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📅 *Due This Week ({len(due_items)}):*\n" + "\n".join(format_item(i) for i in due_items)}})
    if not overdue and not due_items:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "✅ Nothing overdue or due this week. Clean slate!"}})

    bolt_app.client.chat_postMessage(channel=NOTIFY_CHANNEL, text="CEO Office Weekly Digest", blocks=blocks)
    return {"ok": True, "overdue": len(overdue), "due_this_week": len(due_items)}


# Vercel WSGI entry point
app = flask_app

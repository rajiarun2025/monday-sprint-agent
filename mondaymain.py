# file: sprint_risk_summary.py
# Generates a risk-aware summary for a selected Sprint group on your Monday board
# and posts the summary back as an update (LLM + risk rules).

import os
import json
import requests
from datetime import datetime, date
from dotenv import load_dotenv

# -----------------------------
# Setup
# -----------------------------
load_dotenv()
API_TOKEN = os.getenv("MONDAY_API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

URL = "https://api.monday.com/v2"
HEADERS = {"Authorization": API_TOKEN, "Content-Type": "application/json"}

# IMPORTANT: set to the main board ID from your browser URL
BOARD_ID = 18327136960  # <-- change if your board id differs

PAGE_LIMIT = 100  # you can go higher (max 500); keeping reasonable

# Column title mapping (case-insensitive) based on your board
COLUMN_TITLE_MAP = {
    "PRODUCT_STATUS": ["product status"],   # status-type
    "DESIGN_STATUS":  ["design status"],    # status-type
    "DEV_STATUS":     ["dev status"],       # status-type
    "PRIORITY":       ["priority"],         # status-type
    "PRODUCT_OWNER":  ["product owner"],    # people-type
    "DESIGNER":       ["designer"],         # people-type
    "DEVELOPER":      ["developer"],        # people-type
    "TIMELINE":       ["timeline"],         # timeline-type
    "PROGRESS":       ["progress"],         # number/status (optional in summary)
    "USE_CASE":       ["use case"],         # optional context
    "VERTICAL":       ["vertical"],         # optional context

    # Status column whose red label is used to highlight missing fields
    # e.g. create a Status column called "Risk Highlight" and set label "Missing fields" to red.
    "RISK_HIGHLIGHT": ["risk highlight", "risk status", "data quality"],
}

DONE_STATUS_ALIASES = {"done", "complete", "released"}
BLOCKED_ALIASES     = {"blocked", "stuck"}
IN_PROGRESS_ALIASES = {"in progress", "working on it"}

# -----------------------------
# Util
# -----------------------------
def gql(query: str, timeout: int = 30):
    """Perform a GraphQL POST call to Monday."""
    resp = requests.post(URL, json={"query": query}, headers=HEADERS, timeout=timeout)
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {json.dumps(data['errors'], indent=2)}")
    return data


def safe_json(value):
    """Return a JSON-escaped string literal for GraphQL, keeping real Unicode."""
    # ensure_ascii=False keeps emojis & non-ASCII as real UTF-8 characters
    return json.dumps(value, ensure_ascii=False)


def norm(s: str) -> str:
    return (s or "").strip().lower()


def parse_people(value_json: str):
    """Extract people IDs from people column value JSON."""
    if not value_json:
        return []
    try:
        parsed = json.loads(value_json)
        arr = parsed.get("personsAndTeams") or parsed.get("personsAndTeamsV2") or []
        ids = [str(p.get("id")) for p in arr if p.get("id") is not None]
        return ids
    except Exception:
        return []


def parse_date_text(text: str):
    """Parse YYYY-MM-DD to date object; else None."""
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def parse_timeline_value(value_json: str):
    """
    Parse Monday.com timeline JSON -> (start_date, end_date).

    Monday usually sends something like:
      {"from": "2025-11-03", "to": "2025-11-19", "timezone": "Asia/Kolkata"}

    But older/other formats may use startDate/endDate or start_date/end_date.
    """
    if not value_json:
        return (None, None)
    try:
        v = json.loads(value_json)
        if not isinstance(v, dict):
            return (None, None)

        # try multiple possible key names
        sd = v.get("from") or v.get("startDate") or v.get("start_date")
        ed = v.get("to")   or v.get("endDate")   or v.get("end_date")

        s = datetime.strptime(sd[:10], "%Y-%m-%d").date() if sd else None
        e = datetime.strptime(ed[:10], "%Y-%m-%d").date() if ed else None
        return (s, e)
    except Exception:
        return (None, None)

# -----------------------------
# Column ID resolution by titles (from board.columns)
# -----------------------------
def find_column_id_by_titles(columns, title_list):
    """Find first matching column ID for any title in title_list (case-insensitive)."""
    wanted = {t.lower() for t in title_list}
    for c in columns:
        if norm(c.get("title")) in wanted:
            return c["id"]
    return None


def map_board_columns(columns):
    resolved = {}
    for key, titles in COLUMN_TITLE_MAP.items():
        resolved[key] = find_column_id_by_titles(columns, titles)
    return resolved

# -----------------------------
# 1) Ask sprint number & fetch board metadata
# -----------------------------
sprint_str = input("Enter Sprint number (e.g., 4): ").strip()
if not sprint_str.isdigit():
    raise ValueError("Sprint number must be a positive integer, e.g., 4.")
sprint_num = int(sprint_str)

meta_q = f"""
query {{
  boards(ids: [{BOARD_ID}]) {{
    id
    name
    groups {{ id title }}
    columns {{ id title type }}
    items_page(limit: {PAGE_LIMIT}) {{
      cursor
      items {{
        id
        name
        group {{ id title }}
        column_values {{
          id
          type
          text
          value
        }}
      }}
    }}
  }}
}}
"""
meta = gql(meta_q)
board = meta["data"]["boards"][0]
board_name = board["name"]
groups = board["groups"]
columns = board["columns"]

# Identify the sprint group by title pattern "Sprint X ..."
sprint_group = None
for g in groups:
    if norm(g.get("title")).startswith(f"sprint {sprint_num}"):
        sprint_group = g
        break

if not sprint_group:
    raise RuntimeError(f"No group starting with 'Sprint {sprint_num}' found on board '{board_name}'.")

sprint_group_id = sprint_group["id"]
sprint_group_title = sprint_group["title"]

column_ids = map_board_columns(columns)

# -----------------------------
# 2) Fetch all items via cursor pagination and filter by sprint group
# -----------------------------
items_all = []
first_page = board.get("items_page", {}) or {}
cursor = first_page.get("cursor")
items_all += first_page.get("items", []) or []

while cursor:
    next_q = f"""
    query {{
      next_items_page(limit: {PAGE_LIMIT}, cursor: "{cursor}") {{
        cursor
        items {{
          id
          name
          group {{ id title }}
          column_values {{
            id
            type
            text
            value
          }}
        }}
      }}
    }}
    """
    nxt = gql(next_q)
    nip = nxt["data"]["next_items_page"]
    items_all += nip.get("items", []) or []
    cursor = nip.get("cursor")

# Only items in the selected sprint group, excluding the summary row(s)
sprint_items = [
    i for i in items_all
    if i.get("group", {}).get("id") == sprint_group_id
    and not norm(i.get("name", "")).startswith("sprint summary")
]

if not sprint_items:
    raise RuntimeError(f"No items found in group '{sprint_group_title}'. Add items or check permissions.")

# -----------------------------
# 3) Risk assessment tailored to your columns
# -----------------------------
today = date.today()
PRIORITY_COL_ID       = column_ids["PRIORITY"]
PRODUCT_STATUS_COL_ID = column_ids["PRODUCT_STATUS"]
DESIGN_STATUS_COL_ID  = column_ids["DESIGN_STATUS"]
DEV_STATUS_COL_ID     = column_ids["DEV_STATUS"]
PRODUCT_OWNER_COL_ID  = column_ids["PRODUCT_OWNER"]
DESIGNER_COL_ID       = column_ids["DESIGNER"]
DEVELOPER_COL_ID      = column_ids["DEVELOPER"]
TIMELINE_COL_ID       = column_ids["TIMELINE"]
RISK_HIGHLIGHT_COL_ID = column_ids["RISK_HIGHLIGHT"]  # may be None if column not present


def cv_lookup(item):
    return {c["id"]: c for c in item.get("column_values", [])}


def status_norm(cv, col_id):
    return norm(cv.get(col_id, {}).get("text") if col_id else "")


def item_risk(item):
    cv = cv_lookup(item)
    name = item["name"]

    product_status = status_norm(cv, PRODUCT_STATUS_COL_ID)
    design_status  = status_norm(cv, DESIGN_STATUS_COL_ID)
    dev_status     = status_norm(cv, DEV_STATUS_COL_ID)

    product_owner_ids = parse_people(cv.get(PRODUCT_OWNER_COL_ID, {}).get("value") if PRODUCT_OWNER_COL_ID else "")
    designer_ids      = parse_people(cv.get(DESIGNER_COL_ID, {}).get("value") if DESIGNER_COL_ID else "")
    developer_ids     = parse_people(cv.get(DEVELOPER_COL_ID, {}).get("value") if DEVELOPER_COL_ID else "")

    priority_text = status_norm(cv, PRIORITY_COL_ID)

    tl_value = cv.get(TIMELINE_COL_ID, {}).get("value") if TIMELINE_COL_ID else ""
    _, tl_end = parse_timeline_value(tl_value)

    reasons = []

    # Blocked/Stuck across tracks
    track_statuses = [product_status, design_status, dev_status]
    if any(s in BLOCKED_ALIASES for s in track_statuses):
        reasons.append("blocked/stuck in one or more tracks")

    # Missing roles (these are what we'll highlight in red)
    if PRODUCT_OWNER_COL_ID and not product_owner_ids:
        reasons.append("missing Product owner")
    if DESIGNER_COL_ID and not designer_ids:
        reasons.append("missing Designer")
    if DEVELOPER_COL_ID and not developer_ids:
        reasons.append("missing Developer")

    # Timeline overdue / near due
    if tl_end and tl_end < today and not any(s in DONE_STATUS_ALIASES for s in track_statuses):
        reasons.append("timeline end passed")
    if tl_end and (tl_end - today).days <= 3 and not any(s in DONE_STATUS_ALIASES for s in track_statuses):
        reasons.append("near due (≤3 days) and not done")

    # High priority + near due
    if "high" in priority_text and tl_end and (tl_end - today).days <= 3 and not any(s in DONE_STATUS_ALIASES for s in track_statuses):
        reasons.append("high priority near due and not done")

    risky = len(reasons) > 0

    return {
        "id": item["id"],
        "name": name,
        "product_status": product_status,
        "design_status": design_status,
        "dev_status": dev_status,
        "priority": priority_text,
        "timeline_end": tl_end.isoformat() if tl_end else "",
        "reasons": reasons,
        "risky": risky,
    }


assessed = [item_risk(i) for i in sprint_items]
risks = [r for r in assessed if r["risky"]]

# -----------------------------
# 3b) Highlight missing fields in a red status (if column exists)
# -----------------------------
def apply_missing_field_highlights(items_assessed):
    """
    For any item with 'missing ...' in its reasons,
    set the RISK_HIGHLIGHT status column to 'Missing fields'.

    In Monday UI, set that label to red for a strong visual.
    """
    if not RISK_HIGHLIGHT_COL_ID:
        return  # nothing to do; column not on this board

    for r in items_assessed:
        missing_reasons = [reason for reason in r["reasons"] if "missing" in reason.lower()]
        if not missing_reasons:
            continue

        item_id = r["id"]
        # Inner payload is JSON string {"label": "Missing fields"}
        status_payload = json.dumps({"label": "Missing fields"})
        value_literal = safe_json(status_payload)

        mut = f"""
        mutation {{
          change_column_value(
            board_id: {BOARD_ID},
            item_id: {item_id},
            column_id: "{RISK_HIGHLIGHT_COL_ID}",
            value: {value_literal}
          ) {{
            id
          }}
        }}
        """
        gql(mut)


apply_missing_field_highlights(assessed)

# -----------------------------
# 4) Build context (including sprint-level timeline)
# -----------------------------
def build_context(items_assessed, risks_list):
    def done_all(r):
        """
        Treat an item as 'done' only if all non-empty track statuses
        (product / design / dev) are in DONE_STATUS_ALIASES.
        """
        statuses = [
            r.get("product_status", ""),
            r.get("design_status", ""),
            r.get("dev_status", ""),
        ]
        present = [s for s in statuses if s]  # ignore empty strings
        if not present:
            return False
        return all(s in DONE_STATUS_ALIASES for s in present)

    total_items = len(items_assessed)
    done_items = sum(1 for r in items_assessed if done_all(r))
    blocked_items = sum(
        1
        for r in items_assessed
        if any(
            s in BLOCKED_ALIASES
            for s in [r["product_status"], r["design_status"], r["dev_status"]]
        )
    )
    high_priority = sum(1 for r in items_assessed if "high" in r["priority"])

    # derive sprint_end from the latest timeline_end across items
    tl_dates = [
        parse_date_text(r["timeline_end"])
        for r in items_assessed
        if r["timeline_end"]
    ]
    sprint_end = max(tl_dates) if tl_dates else None

    # items that missed their own timeline and are not done
    late_items = []
    for r in items_assessed:
        if not r["timeline_end"]:
            continue
        d = parse_date_text(r["timeline_end"])
        if not d:
            continue
        if d < today and not done_all(r):
            late_items.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "timeline_end": r["timeline_end"],
                    "product_status": r["product_status"],
                    "design_status": r["design_status"],
                    "dev_status": r["dev_status"],
                }
            )

    if sprint_end is None:
        sprint_timeline_status = "unknown"
    else:
        if today < sprint_end:
            sprint_timeline_status = "ongoing"
        else:
            # after (or on) sprint_end: check if everything is done
            if done_items == total_items and total_items > 0:
                sprint_timeline_status = "met"
            else:
                sprint_timeline_status = "missed"

    stats = {
        "total_items": total_items,
        "risky_items": len(risks_list),
        "done_items": done_items,
        "blocked_items": blocked_items,
        "high_priority": high_priority,
    }

    return {
        "sprint_group": sprint_group_title,
        "stats": stats,
        "top_risks": risks_list[:10],
        "timeline": {
            "sprint_end": sprint_end.isoformat() if sprint_end else "",
            "status": sprint_timeline_status,
            "late_items": late_items,
        },
    }


context = build_context(assessed, risks)

# -----------------------------
# 5) LLM summary (or fallback)
# -----------------------------
def generate_llm_summary(ctx):
    prompt = (
        "You are an Agile PM assistant. Using the structured context below, "
        "produce a crisp sprint summary ONLY for the selected sprint group. "
        "Include totals, at-risk counts, key risks with reasons (top 5), and 2–3 actions. "
        "Use the 'timeline.status' and 'timeline.sprint_end' fields to explain whether "
        "the sprint timeline was MET, MISSED, or is ONGOING. If it is MISSED, "
        "explicitly name the items in 'timeline.late_items' that were not completed by "
        "the sprint end date, and which track(s) are still open (product/design/dev). "
        "Keep it under ~250 words.\n\n"
        f"CONTEXT:\n{json.dumps(ctx, indent=2)}\n"
    )

    if not OPENAI_API_KEY:
        tl = ctx.get("timeline", {})
        sprint_end = tl.get("sprint_end") or "N/A"
        tl_status = tl.get("status") or "unknown"
        late_items = tl.get("late_items") or []

        lines = [
            f"Sprint Summary for {ctx['sprint_group']} ({datetime.now().strftime('%Y-%m-%d')}):",
            f"- Total items: {ctx['stats']['total_items']}",
            f"- Done: {ctx['stats']['done_items']}",
            f"- Blocked: {ctx['stats']['blocked_items']}",
            f"- High priority: {ctx['stats']['high_priority']}",
            f"- At risk: {ctx['stats']['risky_items']}",
            f"- Sprint end (from item timelines): {sprint_end} [{tl_status}]",
            "",
            "Top risks:",
        ]

        for r in ctx["top_risks"][:5]:
            lines.append(
                f"• {r['name']} — {', '.join(r['reasons'])} | "
                f"Priority: {r['priority']} | Timeline end: {r['timeline_end']}"
            )

        if tl_status == "missed" and late_items:
            lines += [
                "",
                "Items not completed by sprint end:",
            ]
            for li in late_items:
                lines.append(
                    f"• {li['name']} (due {li['timeline_end']}) — "
                    f"Product: {li['product_status'] or '-'}, "
                    f"Design: {li['design_status'] or '-'}, "
                    f"Dev: {li['dev_status'] or '-'}"
                )

        lines += [
            "",
            "Actions:",
            "- Assign missing Product owner/Designer/Developer (🔴 items).",
            "- Clear blockers across Product/Design/Dev; escalate stalled items.",
            "- Focus high-priority items due within 3 days; replan if needed.",
        ]
        return "\n".join(lines)

    # OpenAI path
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[
                {"role": "system", "content": "You are an expert Agile PM assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"(LLM error: {e})\n\nFallback:\n" + json.dumps(ctx, indent=2)


summary_text = generate_llm_summary(context)

# -----------------------------
# 6) Create OR REUSE summary item & post update
# -----------------------------
summary_item_name = f"Sprint Summary - {datetime.now().strftime('%Y-%m-%d')}"

# Look for existing sprint summary item in this group
existing_summary_items = [
    i for i in items_all
    if i.get("group", {}).get("id") == sprint_group_id
    and norm(i.get("name", "")).startswith("sprint summary")
]

if existing_summary_items:
    # Reuse the first existing sprint summary item
    summary_item_id = existing_summary_items[0]["id"]

    # RENAME using change_multiple_column_values
    colvals_json = json.dumps({"name": summary_item_name}, ensure_ascii=False)
    column_values_literal = safe_json(colvals_json)

    rename_mut = f"""
    mutation {{
      change_multiple_column_values(
        board_id: {BOARD_ID},
        item_id: {summary_item_id},
        column_values: {column_values_literal}
      ) {{
        id
      }}
    }}
    """
    gql(rename_mut)
else:
    # No existing summary item -> create a new one
    create_item_mut = f"""
    mutation {{
      create_item(board_id: {BOARD_ID}, group_id: "{sprint_group_id}", item_name: "{summary_item_name}") {{
        id
        name
      }}
    }}
    """
    create_item_resp = gql(create_item_mut)
    summary_item_id = create_item_resp["data"]["create_item"]["id"]

# Post the summary as an update (NOTE: proper 'body' argument)
update_value_literal = safe_json(summary_text)
post_update_mut = f"""
mutation {{
  create_update(item_id: {summary_item_id}, body: {update_value_literal}) {{
    id
  }}
}}
"""
update_resp = gql(post_update_mut)

# -----------------------------
# 7) Output
# -----------------------------
print("\n=== RESULT ===")
print(f"Board: {board_name} (ID: {BOARD_ID})")
print(f"Sprint group: {sprint_group_title} (ID: {sprint_group_id})")
print(f"Items in sprint (excluding summary row): {len(sprint_items)}")
print(f"Risky items: {len(risks)}")
print(f"Summary item ID: {summary_item_id}")
print(f"Update posted ID: {update_resp['data']['create_update']['id']}")
print("\nSummary Preview:\n")
print(summary_text)

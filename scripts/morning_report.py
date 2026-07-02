#!/usr/bin/env python3
"""Deterministic morning health summary from a garmin-mcp server.

Connects to a garmin-mcp streamable-http endpoint as an MCP client, pulls
today's key metrics, and prints a Slack-mrkdwn-formatted summary to stdout.
Stdlib only — runs anywhere with Python 3.9+ and no installs.

Usage:
    python3 morning_report.py --url https://host/mcp-<secret> [--tz Europe/Oslo]

Intended for scheduled agents/cron: the script owns the message content, the
caller just delivers stdout. Exits non-zero if no metrics could be fetched.
"""

import argparse
import datetime
import json
import sys
import urllib.request

PROTOCOL = "2025-03-26"


class MCPClient:
    """Minimal MCP streamable-http client (initialize + tools/call)."""

    def __init__(self, url):
        self.url = url
        self.session_id = None
        self._id = 0

    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            body = resp.read().decode()
            ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            # take the last data: line of the SSE stream
            data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
            return json.loads(data_lines[-1]) if data_lines else None
        return json.loads(body) if body.strip() else None

    def _rpc(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        out = self._post(msg)
        if out and "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return (out or {}).get("result")

    def connect(self):
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "morning-report", "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, args):
        result = self._rpc("tools/call", {"name": tool, "arguments": args}) or {}
        for item in result.get("content", []):
            if item.get("type") == "text":
                text = item["text"]
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text  # tool returned a plain message (e.g. "No data found")
        return None


def fetch(client, tool, args):
    """Call a tool, returning None on any failure so one bad metric never kills the report."""
    try:
        out = client.call(tool, args)
        return out if isinstance(out, (dict, list)) else None
    except Exception as e:
        print(f"warn: {tool}: {e}", file=sys.stderr)
        return None


def hm(seconds):
    return f"{int(seconds // 3600)}:{round(seconds % 3600 / 60):02d}" if seconds else None


def tidy(level):
    return level.replace("_", " ").title() if isinstance(level, str) else None


def build_report(client, today, yesterday):
    readiness = fetch(client, "get_training_readiness", {"date": today}) or []
    if isinstance(readiness, dict):
        readiness = [readiness]
    r0 = readiness[0] if readiness else {}
    sleep = fetch(client, "get_sleep_summary", {"date": today}) or {}
    hrv = fetch(client, "get_hrv_data", {"date": today}) or {}
    stats = fetch(client, "get_stats", {"date": today}) or {}
    ysteps_list = fetch(client, "get_daily_steps",
                        {"start_date": yesterday, "end_date": yesterday}) or []
    ysteps = ysteps_list[0] if isinstance(ysteps_list, list) and ysteps_list else {}

    # Everything an interpretation layer might reason over (used by --json)
    metrics = {"date": today, "readiness": r0, "sleep": sleep, "hrv": hrv,
               "stats": stats, "yesterday_steps": ysteps}

    date_h = datetime.date.fromisoformat(today).strftime("%A %d %B").replace(" 0", " ")
    lines = [f"*Garmin morning report — {date_h}*"]

    head = []
    if r0.get("score") is not None:
        head.append(f"Readiness *{r0['score']}*/100 ({tidy(r0.get('level')) or '–'})")
    if sleep.get("sleep_score") is not None:
        dur = hm(sleep.get("sleep_seconds"))
        head.append(f"Sleep *{sleep['sleep_score']}*/100" + (f" ({dur} h)" if dur else ""))
    if hrv.get("last_night_avg_hrv_ms") is not None:
        head.append(f"HRV *{hrv['last_night_avg_hrv_ms']} ms* ({tidy(hrv.get('status')) or '–'})")
    if stats.get("resting_heart_rate_bpm") is not None:
        head.append(f"RHR *{stats['resting_heart_rate_bpm']} bpm*")
    if head:
        lines.append(" · ".join(head))

    extras = []
    bb_now = stats.get("body_battery_current")
    if bb_now is not None:
        rng = ""
        if stats.get("body_battery_lowest") is not None:
            rng = f" ({stats['body_battery_lowest']}–{stats['body_battery_highest']} today)"
        extras.append(f"Body battery {bb_now}{rng}")
    y_total = ysteps.get("totalSteps")
    if y_total is not None:
        goal = ysteps.get("stepGoal")
        mark = " ✅" if goal and y_total >= goal else ""
        extras.append(f"Yesterday: {y_total:,} steps{mark}"
                      + (f" (goal {goal:,})" if goal else ""))
    if extras:
        lines.append(" · ".join(extras))

    if len(lines) == 1:
        return None, metrics
    return "\n".join(lines), metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="garmin-mcp streamable-http endpoint URL")
    ap.add_argument("--tz", default="Europe/Oslo", help="IANA timezone for 'today'")
    ap.add_argument("--json", action="store_true",
                    help="emit {report, metrics} JSON — for callers that add an "
                         "interpretation layer on top of the deterministic report")
    args = ap.parse_args()

    from zoneinfo import ZoneInfo
    today = datetime.datetime.now(ZoneInfo(args.tz)).date()
    yesterday = today - datetime.timedelta(days=1)

    client = MCPClient(args.url)
    client.connect()
    report, metrics = build_report(client, today.isoformat(), yesterday.isoformat())
    if not report:
        sys.exit("No metrics could be fetched — is the garmin-mcp server healthy?")
    if args.json:
        print(json.dumps({"report": report, "metrics": metrics}, indent=1))
    else:
        print(report)


if __name__ == "__main__":
    main()

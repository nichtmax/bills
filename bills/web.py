"""Lightweight Flask web UI for the bills app.

Served in a daemon thread alongside the scheduler (same process), so both share
the RunManager and the file-backed config. Server-rendered, no frontend build.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)

from croniter import croniter
from datetime import datetime

from . import db
from .config import (
    DEFAULT_CRON,
    SETTINGS_SCHEMA,
    Config,
    load_settings,
    save_settings,
)
from .core.mailer import Mailer
from .invoices import delete_invoice, list_invoices, mail_invoice, resolve_pdf_path
from .runner import GLOBAL as runner

LAYOUT_TOP = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bills</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; background: #0f1115; color: #e6e6e6; }
  a { color: #6ea8fe; text-decoration: none; }
  nav { background: #161a22; padding: 12px 20px; display: flex; gap: 18px; align-items: center;
        border-bottom: 1px solid #262b36; flex-wrap: wrap; }
  nav .brand { font-weight: 700; font-size: 18px; margin-right: 10px; }
  nav .nav-spacer { flex: 1; }
  .container { width: 100%; max-width: none; margin: 22px 0; padding: 0 16px; }
  .card { background: #161a22; border: 1px solid #262b36; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 18px; }
  h1 { font-size: 20px; } h2 { font-size: 16px; margin: 0 0 12px; }
  label { display: block; font-size: 13px; margin: 10px 0 4px; color: #aab; }
  input[type=text], input[type=password], select, textarea { width: 100%; padding: 8px 10px;
          background: #0f1115; border: 1px solid #333a47; border-radius: 6px; color: #e6e6e6; }
  textarea { min-height: 96px; resize: vertical; font-family: inherit; }
  .btn, button { display: inline-flex; align-items: center; justify-content: center; gap: 6px;
           background: #2b6cff; color: #fff; border: 1px solid transparent; padding: 8px 14px;
           border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500;
           text-decoration: none; line-height: 1.2; transition: background .15s, transform .1s, border-color .15s; }
  .btn:hover, button:hover { filter: brightness(1.08); }
  .btn:active, button:active { transform: scale(0.97); }
  .btn .sym, button .sym { font-size: 15px; line-height: 1; opacity: .95; }
  .btn.secondary, button.secondary { background: #2a3140; border-color: #3d4659; color: #e6e6e6; }
  .btn.success, button.success { background: #1a5c38; border-color: #267a4d; }
  .btn.danger, button.danger, button.warn { background: #8b2e2e; border-color: #a33; }
  .btn.ghost, a.btn.ghost { background: #1a2030; border-color: #3d4659; color: #8ab4f8; }
  .btn.sm, button.sm { padding: 6px 10px; font-size: 12px; border-radius: 7px; }
  .btn.icon-only, button.icon-only { padding: 7px 9px; min-width: 34px; }
  .btn.icon-only .sym, button.icon-only .sym { font-size: 16px; }
  .btn-group { display: inline-flex; gap: 5px; flex-wrap: wrap; align-items: center; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  pre { background: #0b0d11; border: 1px solid #262b36; border-radius: 6px; padding: 10px;
        max-height: 320px; overflow: auto; font-size: 12px; white-space: pre-wrap; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .b-idle { background: #2a2f3a; color: #aab; }
  .b-running { background: #6b4a00; color: #ffd479; }
  .b-success { background: #14502a; color: #7be0a3; }
  .b-failed { background: #5a1620; color: #ffb4b4; }
  .b-tracked { background: #14502a; color: #7be0a3; }
  .b-file-only { background: #2a3a5a; color: #8ab4f8; }
  .b-missing { background: #5a4a16; color: #ffd479; }
  .muted { color: #889; font-size: 12px; }
  .flash { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; }
  .flash.ok { background: #14502a; } .flash.err { background: #5a1620; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #262b36; }
  tr.inv-older { display: none; }
  tr.inv-older.show { display: table-row; }
  tr.inv-expand td { border-bottom: 1px solid #262b36; padding-top: 0; padding-bottom: 10px; }
</style>
</head>
<body>
<nav>
  <span class="brand">📋 bills</span>
  <a href="{{ url_for('dashboard') }}">⌂ Dashboard</a>
  <a href="{{ url_for('invoices_view') }}">📄 Invoices</a>
  <a href="{{ url_for('config_view') }}">⚙ Config</a>
  <span class="nav-spacer"></span>
  <button type="button" class="btn secondary sm" id="notify-enable" style="display:none"
          title="Browser notifications when addon runs finish">
    <span class="sym">🔔</span> <span id="notify-label">Enable</span>
  </button>
</nav>
<div class="container">
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat, m in msgs %}
      <div class="flash {{ 'ok' if cat == 'ok' else 'err' }}">{{ m }}</div>
    {% endfor %}
  {% endwith %}
"""

LAYOUT_BOT = """
</div>
<script>
(function() {
  const lastRunState = {};
  let notifyInitialized = false;
  let notificationsEnabled = localStorage.getItem("bills-notifications") === "1";

  function parseRunSummary(log) {
    const lines = String(log || "").split("\\n");
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/=== (\\w+) done: downloaded=(\\d+) skipped=(\\d+) failed=(\\d+) ===/);
      if (m) {
        return {
          addon: m[1],
          downloaded: Number(m[2]),
          skipped: Number(m[3]),
          failed: Number(m[4]),
        };
      }
    }
    return null;
  }

  function updateNotifyButton() {
    const btn = document.getElementById("notify-enable");
    const label = document.getElementById("notify-label");
    if (!btn || !label) return;
    if (!("Notification" in window)) {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "inline-flex";
    btn.classList.remove("success", "secondary");
    if (Notification.permission === "granted" && notificationsEnabled) {
      btn.classList.add("success");
      label.textContent = "On";
      btn.title = "Browser notifications enabled";
    } else if (Notification.permission === "denied") {
      btn.classList.add("secondary");
      label.textContent = "Blocked";
      btn.disabled = true;
      btn.title = "Notifications blocked in browser settings";
    } else {
      btn.classList.add("secondary");
      label.textContent = "Enable";
      btn.disabled = false;
      btn.title = "Enable browser notifications for completed runs";
    }
  }

  async function requestNotificationPermission() {
    if (!("Notification" in window)) return false;
    if (Notification.permission === "granted") {
      notificationsEnabled = true;
      localStorage.setItem("bills-notifications", "1");
      updateNotifyButton();
      return true;
    }
    if (Notification.permission === "denied") {
      updateNotifyButton();
      return false;
    }
    const permission = await Notification.requestPermission();
    notificationsEnabled = permission === "granted";
    if (notificationsEnabled) {
      localStorage.setItem("bills-notifications", "1");
    } else {
      localStorage.removeItem("bills-notifications");
    }
    updateNotifyButton();
    return notificationsEnabled;
  }

  function showRunNotification(addon, status) {
    if (!notificationsEnabled || Notification.permission !== "granted") return;
    const summary = parseRunSummary(status.log);
    let title;
    let body;
    if (status.state === "failed") {
      title = "bills: " + addon + " failed";
      const tail = String(status.log || "").split("\\n").filter(Boolean).slice(-2).join(" · ");
      body = tail || "Run failed";
    } else if (summary && summary.downloaded > 0) {
      title = "bills: " + summary.downloaded + " new invoice(s)";
      body = addon + " downloaded " + summary.downloaded + ", skipped " + summary.skipped;
    } else if (summary && summary.failed > 0) {
      title = "bills: " + addon + " finished with errors";
      body = "downloaded " + summary.downloaded + ", failed " + summary.failed;
    } else {
      title = "bills: " + addon + " complete";
      body = summary
        ? "No new invoices (skipped " + summary.skipped + ")"
        : "Run finished successfully";
    }
    try {
      new Notification(title, {
        body: body,
        tag: "bills-run-" + addon + "-" + status.finished,
      });
    } catch (e) {}
  }

  function updateDashboard(statusMap) {
    const addons = window.BILLS_DASHBOARD_ADDONS || Object.keys(statusMap);
    for (const addon of addons) {
      const status = statusMap[addon];
      const badge = document.getElementById("badge-" + addon);
      const meta = document.getElementById("meta-" + addon);
      const log = document.getElementById("log-" + addon);
      if (!status || !badge || !meta || !log) continue;
      badge.textContent = status.state;
      badge.className = "badge b-" + status.state;
      let metaText = "";
      if (status.started) metaText += "started " + status.started;
      if (status.finished) metaText += " · finished " + status.finished;
      if (status.returncode !== null && status.returncode !== undefined) {
        metaText += " · rc=" + status.returncode;
      }
      if (status.trigger) metaText += " · " + status.trigger;
      meta.textContent = metaText || "—";
      if (status.log) {
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.textContent = status.log;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
    }
  }

  async function pollRuns() {
    try {
      const response = await fetch("/api/runs");
      const data = await response.json();
      updateDashboard(data);
      for (const [addon, status] of Object.entries(data)) {
        if (!notifyInitialized) {
          lastRunState[addon] = { state: status.state, finished: status.finished };
          continue;
        }
        const prev = lastRunState[addon];
        if (
          prev &&
          prev.state === "running" &&
          status.state !== "running" &&
          status.finished &&
          status.finished !== prev.finished
        ) {
          showRunNotification(addon, status);
        }
        lastRunState[addon] = { state: status.state, finished: status.finished };
      }
      notifyInitialized = true;
    } catch (e) {}
  }

  const notifyBtn = document.getElementById("notify-enable");
  if (notifyBtn) {
    notifyBtn.addEventListener("click", requestNotificationPermission);
  }
  updateNotifyButton();
  if (notificationsEnabled && Notification.permission === "granted") {
    // already enabled
  } else if (notificationsEnabled && Notification.permission === "default") {
    notificationsEnabled = false;
    localStorage.removeItem("bills-notifications");
    updateNotifyButton();
  }
  pollRuns();
  setInterval(pollRuns, 2000);
})();
</script>
</body>
</html>
"""

DASHBOARD = LAYOUT_TOP + """
<div class="card">
  <h1>Bill automation</h1>
  <p class="muted">Download root: {{ download_root }} &middot; Config: {{ config_dir }} &middot;
     Browser: Playwright Chromium &middot; Enabled: {{ enabled|join(', ') }}</p>
  <div class="btn-group">
  <form method="post" action="{{ url_for('run', target='all') }}" style="display:inline">
    <button type="submit" title="Run all enabled addons"><span class="sym">⟳</span> Check all</button>
  </form>
  {% for a in enabled %}
  <form method="post" action="{{ url_for('run', target=a) }}" style="display:inline">
    <button class="secondary sm" type="submit" title="Check {{ a }} for new invoices"><span class="sym">▶</span> {{ a }}</button>
  </form>
  {% endfor %}
  </div>
</div>

<div class="card">
  <h2>Send mail</h2>
  <p class="muted">All addons use the same SMTP settings from Config.</p>
  <form method="post" action="{{ url_for('mail_test') }}" class="row">
    <button type="submit" title="Send SMTP test message"><span class="sym">✉</span> Test email</button>
  </form>
  <div class="btn-group" style="margin-top:10px">
    {% for a in known %}
    <form method="post" action="{{ url_for('mail_resend', addon=a) }}" style="display:inline">
      <button class="secondary sm" type="submit" title="Re-send latest {{ a }} invoice"><span class="sym">↻</span> {{ a }}</button>
    </form>
    {% endfor %}
  </div>
</div>

{% for a in known %}
<div class="card">
  <div class="row" style="justify-content:space-between">
    <h2 style="margin:0">{{ a }}</h2>
    <span class="badge b-idle" id="badge-{{ a }}">idle</span>
  </div>
  <p class="muted" id="meta-{{ a }}">no runs yet this session</p>
  <pre id="log-{{ a }}">(no output yet)</pre>
</div>
{% endfor %}

<script>window.BILLS_DASHBOARD_ADDONS = {{ known|tojson }};</script>
""" + LAYOUT_BOT

INVOICES_PAGE = LAYOUT_TOP + """
<div class="card">
  <h1>Invoices</h1>
  <p class="muted">{{ total }} invoice(s) under {{ download_root }}.
     Showing latest per addon{% if hidden_count %} ({{ hidden_count }} older hidden){% endif %}.</p>
  {% if groups %}
  <table>
    <tr>
      <th>Addon</th><th>Date</th><th>Number</th><th>Added</th><th>Mailed</th><th>Sender protocol</th><th></th>
    </tr>
    {% for addon, invs in groups %}
    {% for r in invs %}
    <tr class="inv-row{% if not loop.first %} inv-older inv-older-{{ addon }}{% endif %}">
      <td>{{ r.addon }}</td>
      <td>{{ r.date }}</td>
      <td>{{ r.number }}</td>
      <td class="muted">{{ r.added }}</td>
      <td class="muted">
        {% if r.mailed %}<span title="Emailed">✓</span> Yes{% if r.mailed_at %}<br><small>{{ r.mailed_at }}</small>{% endif %}{% else %}<span title="Not emailed">○</span> No{% endif %}
      </td>
      <td class="muted">
        {% if r.mail_sender or r.mail_protocol %}
          {% if r.mail_sender %}{{ r.mail_sender }}{% endif %}
          {% if r.mail_protocol %}<br><small>{{ r.mail_protocol }}</small>{% endif %}
        {% else %}—{% endif %}
      </td>
      <td>
        <div class="btn-group">
        {% if r.file_exists %}
        <a class="btn ghost sm icon-only" href="{{ url_for('invoice_view', addon=r.addon, filename=r.filename) }}"
           target="_blank" rel="noopener" title="View PDF in browser"><span class="sym">👁</span></a>
        <a class="btn ghost sm icon-only" href="{{ url_for('invoice_download', addon=r.addon, filename=r.filename) }}"
           title="Download PDF"><span class="sym">⬇</span></a>
        <form method="post" action="{{ url_for('invoice_mail', addon=r.addon, filename=r.filename) }}" style="display:inline">
          <button class="{% if r.mailed %}secondary{% else %}success{% endif %} sm icon-only" type="submit"
                  title="{{ 'Re-send email' if r.mailed else 'Send invoice by email' }}">
            <span class="sym">{% if r.mailed %}↻{% else %}✉{% endif %}</span>
          </button>
        </form>
        {% else %}<span class="muted">—</span>{% endif %}
        <form method="post" action="{{ url_for('invoice_delete', addon=r.addon, filename=r.filename) }}" style="display:inline"
              onsubmit="return confirm('Delete {{ r.filename }}? This cannot be undone.');">
          <button class="danger sm icon-only" type="submit" title="Delete invoice"><span class="sym">🗑</span></button>
        </form>
        </div>
      </td>
    </tr>
    {% endfor %}
    {% if invs|length > 1 %}
    <tr class="inv-expand">
      <td colspan="7">
        <button type="button" class="btn secondary sm inv-toggle inv-toggle-{{ addon }}"
                data-addon="{{ addon }}" data-count="{{ invs|length - 1 }}" onclick="toggleInvoices('{{ addon }}')">
          <span class="sym inv-toggle-icon-{{ addon }}">▸</span>
          <span class="inv-toggle-label-{{ addon }}">Show {{ invs|length - 1 }} older</span>
        </button>
      </td>
    </tr>
    {% endif %}
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">No invoices found yet.</p>
  {% endif %}
</div>
<script>
function toggleInvoices(addon) {
  const rows = document.querySelectorAll('.inv-older-' + addon);
  const btn = document.querySelector('.inv-toggle-' + addon);
  const icon = document.querySelector('.inv-toggle-icon-' + addon);
  const label = document.querySelector('.inv-toggle-label-' + addon);
  const expanded = btn.dataset.expanded === '1';
  rows.forEach(function(r) { r.classList.toggle('show', !expanded); });
  btn.dataset.expanded = expanded ? '0' : '1';
  const count = btn.dataset.count;
  if (expanded) {
    icon.textContent = '▸';
    label.textContent = 'Show ' + count + ' older';
  } else {
    icon.textContent = '▾';
    label.textContent = 'Hide older';
  }
}
</script>
""" + LAYOUT_BOT

CONFIG_PAGE = LAYOUT_TOP + """
<div class="card">
  <h1>Configuration</h1>
  <p class="muted">Values resolve as settings.json &rarr; environment &rarr; default.
     Saving writes {{ config_dir }}/settings.json. Secrets are write-only here.</p>
  <form method="post">
    {% for section in schema %}
      <h2 style="margin-top:18px">{{ section.section }}</h2>
      {% if section.note %}<p class="muted">{{ section.note }}</p>{% endif %}
      <div class="grid2">
      {% for f in section.fields %}
        <div{% if f.type == 'textarea' %} style="grid-column: 1 / -1"{% endif %}>
          <label for="{{ f.key }}">{{ f.label }}
            {% if f.type == 'secret' %}<span class="muted">({{ 'set' if values[f.key] else 'unset' }})</span>{% endif %}
          </label>
          {% if f.type == 'bool' %}
            <input type="checkbox" name="{{ f.key }}" id="{{ f.key }}" {{ 'checked' if values[f.key] else '' }}>
          {% elif f.type == 'secret' %}
            <input type="password" name="{{ f.key }}" id="{{ f.key }}" placeholder="(unchanged)" autocomplete="new-password">
          {% elif f.type == 'textarea' %}
            <textarea name="{{ f.key }}" id="{{ f.key }}" rows="5">{{ values[f.key] }}</textarea>
          {% else %}
            <input type="text" name="{{ f.key }}" id="{{ f.key }}" value="{{ values[f.key] }}">
          {% endif %}
        </div>
      {% endfor %}
      </div>
    {% endfor %}
    <h2 id="schedules" style="margin-top:24px">Schedules</h2>
    <p class="muted">Per-addon cron expressions. Saved to SQLite (<code>bills.db</code>) and picked up within ~30s.</p>
    <table>
      <tr><th>Addon</th><th>Cron</th><th>Next run</th></tr>
      {% for a in addons %}
      <tr>
        <td>{{ a }}</td>
        <td><input type="text" name="cron_{{ a }}" value="{{ crons[a] }}"></td>
        <td class="muted">{{ nexts[a] }}</td>
      </tr>
      {% endfor %}
    </table>
    <p class="muted" style="margin-top:8px">Examples: <code>0 6 * * 1</code> (Mon 06:00),
       <code>0 6 5 * *</code> (5th 06:00), <code>0 6 1 4 *</code> (1 Apr 06:00).</p>
    <div style="margin-top:18px"><button type="submit"><span class="sym">💾</span> Save configuration</button></div>
  </form>
</div>
""" + LAYOUT_BOT


def _known_addons(cfg: Config) -> list[str]:
    return sorted(set(cfg.enabled_addons()) | set(DEFAULT_CRON))


def _group_invoices(rows):
    """Group invoice rows by addon (date-desc within each group)."""
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row.addon, []).append(row)
    return sorted(groups.items(), key=lambda item: item[1][0].sort_key(), reverse=True)


def _schedule_context(cfg: Config) -> tuple[list[str], dict[str, str], dict[str, str]]:
    addons = _known_addons(cfg)
    crons = {a: cfg.cron(a) for a in addons}
    nexts: dict[str, str] = {}
    now = datetime.now()
    for a in addons:
        try:
            nexts[a] = croniter(crons[a], now).get_next(datetime).strftime("%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            nexts[a] = "invalid"
    return addons, crons, nexts


def _save_schedules_from_form(addons: list[str]) -> list[str]:
    errors = []
    for a in addons:
        expr = request.form.get(f"cron_{a}", "").strip()
        if not expr:
            db.delete_schedule(a)
            continue
        if not croniter.is_valid(expr):
            errors.append(f"{a}: invalid cron '{expr}'")
            continue
        db.set_schedule(a, expr)
    return errors


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("BILLS_WEB_SECRET", "bills-local-secret")

    @app.route("/")
    def dashboard():
        cfg = Config()
        return render_template_string(
            DASHBOARD,
            enabled=cfg.enabled_addons(),
            known=_known_addons(cfg),
            download_root=cfg.download_root,
            config_dir=cfg.config_dir,
        )

    @app.route("/api/runs")
    def api_runs():
        return jsonify(runner.all_status())

    @app.route("/run/<target>", methods=["POST"])
    def run(target):
        cfg = Config()
        enabled = cfg.enabled_addons()
        if target == "all":
            runner.run_all_async(enabled, trigger="manual")
            flash(f"Triggered run for: {', '.join(enabled) or '(none enabled)'}", "ok")
        else:
            started = runner.run_async(target, trigger="manual")
            flash(
                f"Triggered {target}" if started else f"{target} is already running",
                "ok" if started else "err",
            )
        return redirect(url_for("dashboard"))

    @app.route("/config", methods=["GET", "POST"])
    def config_view():
        cfg = Config()
        addons, crons, nexts = _schedule_context(cfg)
        if request.method == "POST":
            settings = load_settings()
            for section in SETTINGS_SCHEMA:
                for f in section["fields"]:
                    key, ftype = f["key"], f["type"]
                    if ftype == "bool":
                        settings[key] = "true" if request.form.get(key) == "on" else "false"
                    elif ftype == "secret":
                        val = request.form.get(key, "")
                        if val.strip():
                            settings[key] = val
                    elif ftype == "textarea":
                        val = request.form.get(key, "")
                        if val.strip():
                            settings[key] = val
                        else:
                            settings.pop(key, None)
                    else:
                        val = request.form.get(key, "").strip()
                        if val:
                            settings[key] = val
                        else:
                            settings.pop(key, None)
            save_settings(settings)
            schedule_errors = _save_schedules_from_form(addons)
            if schedule_errors:
                flash("Configuration saved; schedule errors: " + "; ".join(schedule_errors), "err")
            else:
                flash("Configuration saved", "ok")
            return redirect(url_for("config_view") + "#schedules")

        values: dict = {}
        for section in SETTINGS_SCHEMA:
            for f in section["fields"]:
                key, ftype = f["key"], f["type"]
                if ftype == "bool":
                    values[key] = cfg.get_bool(key, False)
                elif ftype == "secret":
                    values[key] = cfg.is_set(key)
                else:
                    raw = cfg.get(key)
                    default = f.get("default", "")
                    values[key] = raw if raw else default
        return render_template_string(
            CONFIG_PAGE,
            schema=SETTINGS_SCHEMA,
            values=values,
            config_dir=cfg.config_dir,
            addons=addons,
            crons=crons,
            nexts=nexts,
        )

    @app.route("/schedule", methods=["GET", "POST"])
    def schedule_view():
        return redirect(url_for("config_view") + "#schedules")

    @app.route("/mail/test", methods=["POST"])
    def mail_test():
        cfg = Config()
        mailer = Mailer(cfg.mail_for())
        ok, msg = mailer.send_text(
            subject="bills test email",
            body="This is a test email from the bills app (shared SMTP config).",
        )
        flash(f"Test email: {msg}", "ok" if ok else "err")
        return redirect(url_for("dashboard"))

    @app.route("/mail/resend/<addon>", methods=["POST"])
    def mail_resend(addon):
        cfg = Config()
        ddir = Path(cfg.download_root) / addon
        pdfs = sorted(ddir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True) if ddir.is_dir() else []
        if not pdfs:
            flash(f"No invoices found for {addon}", "err")
            return redirect(url_for("dashboard"))
        ok, msg = mail_invoice(cfg, addon, pdfs[0].name)
        flash(f"Re-send {pdfs[0].name}: {msg}", "ok" if ok else "err")
        return redirect(url_for("dashboard"))

    @app.route("/invoices")
    def invoices_view():
        cfg = Config()
        rows = list_invoices(cfg)
        groups = _group_invoices(rows)
        hidden_count = sum(len(invs) - 1 for _, invs in groups if len(invs) > 1)
        return render_template_string(
            INVOICES_PAGE,
            groups=groups,
            total=len(rows),
            hidden_count=hidden_count,
            download_root=cfg.download_root,
        )

    @app.route("/invoices/<addon>/<filename>/view")
    def invoice_view(addon, filename):
        cfg = Config()
        path = resolve_pdf_path(cfg, addon, filename)
        if not path:
            flash("Invoice not found", "err")
            return redirect(url_for("invoices_view"))
        return send_file(path, mimetype="application/pdf")

    @app.route("/invoices/<addon>/<filename>")
    def invoice_download(addon, filename):
        cfg = Config()
        path = resolve_pdf_path(cfg, addon, filename)
        if not path:
            flash("Invoice not found", "err")
            return redirect(url_for("invoices_view"))
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=filename)

    @app.route("/invoices/<addon>/<filename>/mail", methods=["POST"])
    def invoice_mail(addon, filename):
        cfg = Config()
        ok, msg = mail_invoice(cfg, addon, filename)
        flash(f"{filename}: {msg}", "ok" if ok else "err")
        return redirect(url_for("invoices_view"))

    @app.route("/invoices/<addon>/<filename>/delete", methods=["POST"])
    def invoice_delete(addon, filename):
        cfg = Config()
        ok, msg = delete_invoice(cfg, addon, filename)
        flash(f"{filename}: {msg}", "ok" if ok else "err")
        return redirect(url_for("invoices_view"))

    return app


def run_web() -> None:
    cfg = Config()
    app = create_app()
    print(f"[web] listening on 0.0.0.0:{cfg.web_port}", flush=True)
    app.run(host="0.0.0.0", port=cfg.web_port, threaded=True, use_reloader=False)


def start_web_in_thread() -> None:
    import threading

    threading.Thread(target=run_web, daemon=True).start()

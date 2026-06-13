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
        border-bottom: 1px solid #262b36; }
  nav .brand { font-weight: 700; font-size: 18px; margin-right: 10px; }
  .container { max-width: 980px; margin: 22px auto; padding: 0 16px; }
  .card { background: #161a22; border: 1px solid #262b36; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 18px; }
  h1 { font-size: 20px; } h2 { font-size: 16px; margin: 0 0 12px; }
  label { display: block; font-size: 13px; margin: 10px 0 4px; color: #aab; }
  input[type=text], input[type=password], select { width: 100%; padding: 8px 10px;
          background: #0f1115; border: 1px solid #333a47; border-radius: 6px; color: #e6e6e6; }
  button { background: #2b6cff; color: #fff; border: 0; padding: 9px 14px; border-radius: 6px;
           cursor: pointer; font-size: 14px; }
  button.secondary { background: #38415280; }
  button.warn { background: #b3541e; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  pre { background: #0b0d11; border: 1px solid #262b36; border-radius: 6px; padding: 10px;
        max-height: 320px; overflow: auto; font-size: 12px; white-space: pre-wrap; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .b-idle { background: #2a2f3a; color: #aab; }
  .b-running { background: #6b4a00; color: #ffd479; }
  .b-success { background: #14502a; color: #7be0a3; }
  .b-tracked { background: #14502a; color: #7be0a3; }
  .b-file-only { background: #2a3a5a; color: #8ab4f8; }
  .b-missing { background: #5a4a16; color: #ffd479; }
  .muted { color: #889; font-size: 12px; }
  .flash { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; }
  .flash.ok { background: #14502a; } .flash.err { background: #5a1620; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #262b36; }
</style>
</head>
<body>
<nav>
  <span class="brand">bills</span>
  <a href="{{ url_for('dashboard') }}">Dashboard</a>
  <a href="{{ url_for('invoices_view') }}">Invoices</a>
  <a href="{{ url_for('config_view') }}">Config</a>
  <a href="{{ url_for('schedule_view') }}">Schedules</a>
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
</body>
</html>
"""

DASHBOARD = LAYOUT_TOP + """
<div class="card">
  <h1>Bill automation</h1>
  <p class="muted">Download root: {{ download_root }} &middot; Config: {{ config_dir }} &middot;
     Browser: Playwright Chromium &middot; Enabled: {{ enabled|join(', ') }}</p>
  <form method="post" action="{{ url_for('run', target='all') }}" style="display:inline">
    <button type="submit">Check all enabled now</button>
  </form>
  {% for a in enabled %}
  <form method="post" action="{{ url_for('run', target=a) }}" style="display:inline">
    <button class="secondary" type="submit">Check {{ a }}</button>
  </form>
  {% endfor %}
</div>

<div class="card">
  <h2>Send mail</h2>
  <form method="post" action="{{ url_for('mail_test') }}" class="row">
    <select name="addon">
      {% for a in known %}<option value="{{ a }}">{{ a }}</option>{% endfor %}
    </select>
    <button type="submit">Send test email</button>
  </form>
  <div class="row" style="margin-top:10px">
    {% for a in known %}
    <form method="post" action="{{ url_for('mail_resend', addon=a) }}" style="display:inline">
      <button class="secondary" type="submit">Re-send latest {{ a }} invoice</button>
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

<script>
const ADDONS = {{ known|tojson }};
async function poll() {
  try {
    const r = await fetch("{{ url_for('api_runs') }}");
    const data = await r.json();
    for (const a of ADDONS) {
      const s = data[a];
      const badge = document.getElementById("badge-" + a);
      const meta = document.getElementById("meta-" + a);
      const log = document.getElementById("log-" + a);
      if (!s) { continue; }
      badge.textContent = s.state;
      badge.className = "badge b-" + s.state;
      let m = "";
      if (s.started) m += "started " + s.started;
      if (s.finished) m += " · finished " + s.finished;
      if (s.returncode !== null && s.returncode !== undefined) m += " · rc=" + s.returncode;
      if (s.trigger) m += " · " + s.trigger;
      meta.textContent = m || "—";
      if (s.log) {
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.textContent = s.log;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
    }
  } catch (e) {}
}
poll();
setInterval(poll, 2000);
</script>
""" + LAYOUT_BOT

INVOICES_PAGE = LAYOUT_TOP + """
<div class="card">
  <h1>Invoices</h1>
  <p class="muted">{{ rows|length }} PDF(s) under {{ download_root }}. Status:
     <span class="badge b-tracked">tracked</span> in SQLite,
     <span class="badge b-file-only">file-only</span> on disk only.</p>
  {% if rows %}
  <table>
    <tr>
      <th>Addon</th><th>Date</th><th>Provider</th><th>Number</th>
      <th>Filename</th><th>Added</th><th>Status</th><th>Mailed</th><th></th>
    </tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.addon }}</td>
      <td>{{ r.date }}</td>
      <td>{{ r.provider }}</td>
      <td>{{ r.number }}</td>
      <td>{{ r.filename }}</td>
      <td class="muted">{{ r.added }}</td>
      <td><span class="badge {% if r.status == 'tracked' %}b-tracked{% elif 'missing' in r.status %}b-missing{% else %}b-file-only{% endif %}">{{ r.status }}</span></td>
      <td class="muted">
        {% if r.mailed %}Yes{% if r.mailed_at %}<br><small>{{ r.mailed_at }}</small>{% endif %}{% else %}No{% endif %}
      </td>
      <td class="row" style="gap:6px">
        {% if r.file_exists %}
        <a href="{{ url_for('invoice_download', addon=r.addon, filename=r.filename) }}">Download</a>
        <form method="post" action="{{ url_for('invoice_mail', addon=r.addon, filename=r.filename) }}" style="display:inline">
          <button class="secondary" type="submit">{{ 'Re-send' if r.mailed else 'Mail' }}</button>
        </form>
        {% else %}—{% endif %}
        <form method="post" action="{{ url_for('invoice_delete', addon=r.addon, filename=r.filename) }}" style="display:inline"
              onsubmit="return confirm('Delete {{ r.filename }}? This cannot be undone.');">
          <button class="warn" type="submit">Delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">No invoices found yet.</p>
  {% endif %}
</div>
""" + LAYOUT_BOT

CONFIG_PAGE = LAYOUT_TOP + """
<div class="card">
  <h1>Configuration</h1>
  <p class="muted">Values resolve as settings.json &rarr; environment &rarr; default.
     Saving writes {{ config_dir }}/settings.json. Secrets are write-only here.</p>
  <form method="post">
    {% for section in schema %}
      <h2 style="margin-top:18px">{{ section.section }}</h2>
      <div class="grid2">
      {% for f in section.fields %}
        <div>
          <label for="{{ f.key }}">{{ f.label }}
            {% if f.type == 'secret' %}<span class="muted">({{ 'set' if values[f.key] else 'unset' }})</span>{% endif %}
          </label>
          {% if f.type == 'bool' %}
            <input type="checkbox" name="{{ f.key }}" id="{{ f.key }}" {{ 'checked' if values[f.key] else '' }}>
          {% elif f.type == 'secret' %}
            <input type="password" name="{{ f.key }}" id="{{ f.key }}" placeholder="(unchanged)" autocomplete="new-password">
          {% else %}
            <input type="text" name="{{ f.key }}" id="{{ f.key }}" value="{{ values[f.key] }}">
          {% endif %}
        </div>
      {% endfor %}
      </div>
    {% endfor %}
    <div style="margin-top:18px"><button type="submit">Save configuration</button></div>
  </form>
</div>
""" + LAYOUT_BOT

SCHEDULE_PAGE = LAYOUT_TOP + """
<div class="card">
  <h1>Schedules</h1>
  <p class="muted">Per-addon cron expressions. Saved to SQLite (<code>bills.db</code> schedules table) and
     picked up by the running scheduler within ~30s.</p>
  <form method="post">
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
    <div style="margin-top:14px"><button type="submit">Save schedules</button></div>
  </form>
  <p class="muted" style="margin-top:12px">Examples: <code>0 6 * * 1</code> (Mon 06:00),
     <code>0 6 1 * *</code> (1st 06:00), <code>*/30 * * * *</code> (every 30 min).</p>
</div>
""" + LAYOUT_BOT


def _known_addons(cfg: Config) -> list[str]:
    return sorted(set(cfg.enabled_addons()) | set(DEFAULT_CRON))


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
                    else:
                        val = request.form.get(key, "").strip()
                        if val:
                            settings[key] = val
                        else:
                            settings.pop(key, None)
            save_settings(settings)
            flash("Configuration saved", "ok")
            return redirect(url_for("config_view"))

        values: dict = {}
        for section in SETTINGS_SCHEMA:
            for f in section["fields"]:
                key, ftype = f["key"], f["type"]
                if ftype == "bool":
                    values[key] = cfg.get_bool(key, False)
                elif ftype == "secret":
                    values[key] = cfg.is_set(key)
                else:
                    values[key] = cfg.get(key)
        return render_template_string(
            CONFIG_PAGE, schema=SETTINGS_SCHEMA, values=values, config_dir=cfg.config_dir
        )

    @app.route("/schedule", methods=["GET", "POST"])
    def schedule_view():
        cfg = Config()
        addons = _known_addons(cfg)
        if request.method == "POST":
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
            if errors:
                flash("; ".join(errors), "err")
            else:
                flash("Schedules saved", "ok")
            return redirect(url_for("schedule_view"))

        crons = {a: cfg.cron(a) for a in addons}
        nexts = {}
        now = datetime.now()
        for a in addons:
            try:
                nexts[a] = croniter(crons[a], now).get_next(datetime).strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                nexts[a] = "invalid"
        return render_template_string(
            SCHEDULE_PAGE, addons=addons, crons=crons, nexts=nexts, config_dir=cfg.config_dir
        )

    @app.route("/mail/test", methods=["POST"])
    def mail_test():
        cfg = Config()
        addon = request.form.get("addon", "vodafone")
        mailer = Mailer(cfg.mail_for(addon))
        ok, msg = mailer.send_text(
            subject=f"bills test email ({addon})",
            body=f"This is a test email from the bills app for the '{addon}' SMTP config.",
        )
        flash(f"Test email ({addon}): {msg}", "ok" if ok else "err")
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
        return render_template_string(
            INVOICES_PAGE,
            rows=rows,
            download_root=cfg.download_root,
        )

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

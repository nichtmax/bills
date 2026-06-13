"""Session login for the bills web UI."""

from __future__ import annotations

import secrets
from functools import wraps

from flask import flash, redirect, request, session, url_for

from .config import Config

LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bills — login</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0f1115; color: #e6e6e6; }
  .card { background: #161a22; border: 1px solid #262b36; border-radius: 10px;
          padding: 24px 28px; width: min(360px, calc(100vw - 32px)); }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p { color: #889; font-size: 13px; margin: 0 0 18px; }
  label { display: block; font-size: 13px; margin: 12px 0 4px; color: #aab; }
  input { width: 100%; padding: 8px 10px; background: #0f1115; border: 1px solid #333a47;
          border-radius: 6px; color: #e6e6e6; box-sizing: border-box; }
  button { margin-top: 18px; width: 100%; background: #2b6cff; color: #fff; border: 0;
           padding: 10px 14px; border-radius: 8px; font-size: 14px; cursor: pointer; }
  .flash { padding: 10px 12px; border-radius: 8px; margin-bottom: 12px; background: #5a1620; }
</style>
</head>
<body>
  <div class="card">
    <h1>📋 bills</h1>
    <p>Sign in to manage invoices and configuration.</p>
    {% if error %}<div class="flash">{{ error }}</div>{% endif %}
    <form method="post">
      <label for="username">Username</label>
      <input type="text" name="username" id="username" autocomplete="username" required>
      <label for="password">Password</label>
      <input type="password" name="password" id="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>
"""


def auth_enabled(cfg: Config | None = None) -> bool:
    cfg = cfg or Config()
    return cfg.is_set("BILLS_WEB_PASSWORD")


def check_credentials(cfg: Config, username: str, password: str) -> bool:
    expected_user = cfg.get("BILLS_WEB_USERNAME", "admin") or "admin"
    expected_pass = cfg.get("BILLS_WEB_PASSWORD")
    if not expected_pass:
        return False
    user_ok = secrets.compare_digest(username.strip(), expected_user)
    pass_ok = secrets.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def register_auth(app) -> None:
    from flask import render_template_string

    @app.context_processor
    def inject_auth():
        cfg = Config()
        return {
            "auth_enabled": auth_enabled(cfg),
            "logged_in": bool(session.get("authenticated")),
        }

    @app.before_request
    def require_login():
        cfg = Config()
        if not auth_enabled(cfg):
            return None
        if request.endpoint in {None, "login", "static"}:
            return None
        if session.get("authenticated"):
            return None
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        cfg = Config()
        if not auth_enabled(cfg):
            return redirect(url_for("dashboard"))
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        error = ""
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if check_credentials(cfg, username, password):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                dest = request.args.get("next") or url_for("dashboard")
                if not str(dest).startswith("/"):
                    dest = url_for("dashboard")
                return redirect(dest)
            error = "Invalid username or password"
        return render_template_string(LOGIN_PAGE, error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        flash("Signed out", "ok")
        if auth_enabled():
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        cfg = Config()
        if not auth_enabled(cfg) or session.get("authenticated"):
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.path))

    return wrapped

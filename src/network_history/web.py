import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from network_history.api import create_api
from network_history.config import Settings
from network_history.storage.repository import Repository


def create_app(settings: Settings, repository: Repository, manual) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=16384,
    )
    timezone = ZoneInfo(settings.timezone)

    @app.template_filter("localtime")
    def localtime(value):
        if value is None:
            return "Never"
        return (
            datetime.fromtimestamp(value, UTC)
            .astimezone(timezone)
            .strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
        )

    @app.before_request
    def csrf():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            submitted = request.form.get("csrf_token", "")
            if not expected or not secrets.compare_digest(expected.encode(), submitted.encode()):
                abort(400, "Invalid form token. Reload the page and try again.")

    @app.context_processor
    def context():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return {
            "csrf_token": session["csrf_token"],
            "state": repository.get_state(),
            "collecting": repository.collection_running(),
            "timezone": settings.timezone,
        }

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def activity():
        # Missing argument means all devices; an empty MAC string is a valid source value.
        mac = request.args.get("mac")
        return render_template(
            "activity.html",
            records=repository.recent_activity(mac=mac),
            devices=repository.list_devices(),
            selected_mac=mac,
        )

    @app.get("/devices")
    def devices():
        return render_template("devices.html", devices=repository.list_devices())

    @app.post("/devices")
    def save_device():
        mac = request.form.get("mac")
        if mac is None:
            abort(400)
        name = request.form.get("friendly_name", "").strip()
        notes = request.form.get("notes", "").strip()
        if len(name) > 200 or len(notes) > 2000:
            abort(400, "Name or notes exceed the allowed length")
        if not repository.update_device(mac, name, notes or None):
            abort(404)
        flash("Device saved.")
        return redirect(url_for("devices"), code=303)

    @app.post("/collect")
    def collect():
        if repository.collection_running():
            flash("A collection is already running.")
        else:
            try:
                launched = manual.start()
            except OSError:
                app.logger.exception("Could not launch manual collector")
                flash("Could not start collection. Check the web service logs.")
            else:
                flash(
                    "Collection requested. Refresh to see its result."
                    if launched
                    else "A collection is already running."
                )
        return redirect(url_for("activity"), code=303)

    @app.get("/health")
    def health():
        repository.get_state()
        return {"status": "ok"}

    app.register_blueprint(create_api(settings, repository))
    return app

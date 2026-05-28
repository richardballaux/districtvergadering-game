import csv
import io
import json
import random
import socket
import os
import uuid
from datetime import datetime

import qrcode
from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from PIL import Image

from game_content import FINISH_MESSAGE, LOCATIONS, TEAM_COLORS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "districtvergadering-leos-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///game.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max upload

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    club = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=True)
    checked_in = db.Column(db.Boolean, default=False)
    session_token = db.Column(db.String(64), nullable=True, unique=True)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    color = db.Column(db.String(20), nullable=False)
    bg_color = db.Column(db.String(20), nullable=False)
    label = db.Column(db.String(20), nullable=False)
    current_step = db.Column(db.Integer, default=0)
    route_json = db.Column(db.String(50), nullable=False, default="[0,1,2,3]")
    finished = db.Column(db.Boolean, default=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    members = db.relationship("Participant", backref="team", lazy=True)
    messages = db.relationship("Message", backref="team", lazy=True,
                               order_by="Message.sent_at")

    @property
    def route(self):
        return json.loads(self.route_json)

    @property
    def current_location(self):
        r = self.route
        if self.current_step < len(r):
            return LOCATIONS[r[self.current_step]]
        return None

    @property
    def progress_pct(self):
        return int((self.current_step / len(LOCATIONS)) * 100)

    @property
    def unread_from_team(self):
        return Message.query.filter_by(
            team_id=self.id, sender_type="team", read_by_admin=False
        ).count()


class LocationProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    location_id = db.Column(db.Integer, nullable=False)
    arrived_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)


class GameSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_started = db.Column(db.Boolean, default=False)
    game_started_at = db.Column(db.DateTime, nullable=True)
    admin_password = db.Column(db.String(100), default="leos2024")
    location_codes_json = db.Column(
        db.String(500),
        default=json.dumps(["CODE1", "CODE2", "CODE3", "CODE4"]),
    )

    @property
    def location_codes(self):
        return json.loads(self.location_codes_json)

    @location_codes.setter
    def location_codes(self, value):
        self.location_codes_json = json.dumps(value)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    sender_type = db.Column(db.String(10), nullable=False)   # 'admin' or 'team'
    sender_name = db.Column(db.String(100), nullable=True)   # participant name when from team
    content = db.Column(db.Text, nullable=True)
    photo_filename = db.Column(db.String(200), nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    read_by_admin = db.Column(db.Boolean, default=False)
    read_by_team = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "sender_type": self.sender_type,
            "sender_name": self.sender_name or "Begeleider",
            "content": self.content,
            "photo_url": url_for("uploaded_file", filename=self.photo_filename)
                         if self.photo_filename else None,
            "sent_at": self.sent_at.strftime("%H:%M"),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_settings():
    s = GameSettings.query.first()
    if not s:
        s = GameSettings()
        db.session.add(s)
        db.session.commit()
    return s


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated


def make_balanced_groups(participants, num_groups):
    by_club = {}
    for p in participants:
        by_club.setdefault(p.club, []).append(p)

    for club in by_club:
        random.shuffle(by_club[club])

    clubs_sorted = sorted(by_club.values(), key=len, reverse=True)
    flat = []
    while any(clubs_sorted):
        for club_members in clubs_sorted:
            if club_members:
                flat.append(club_members.pop(0))
        clubs_sorted = [c for c in clubs_sorted if c]

    groups = [[] for _ in range(num_groups)]
    for i, p in enumerate(flat):
        groups[i % num_groups].append(p)
    return groups


def generate_routes(num_teams):
    base = list(range(len(LOCATIONS)))
    routes = []
    for i in range(num_teams):
        rotated = base[i % len(base):] + base[: i % len(base)]
        routes.append(rotated)
    return routes


def save_photo(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_FOLDER, filename))
    return filename


# ---------------------------------------------------------------------------
# Init DB
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()
    get_settings()


# ---------------------------------------------------------------------------
# Static: uploaded photos
# ---------------------------------------------------------------------------

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))


# ---------------------------------------------------------------------------
# Admin: auth
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        settings = get_settings()
        if pw == settings.admin_password:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Fout wachtwoord."
    return render_template("admin/login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Admin: dashboard
# ---------------------------------------------------------------------------

@app.route("/admin/")
@app.route("/admin")
@admin_required
def admin_dashboard():
    settings = get_settings()
    participant_count = Participant.query.count()
    team_count = Team.query.count()
    checked_in = Participant.query.filter_by(checked_in=True).count()
    teams = Team.query.all()
    total_unread = Message.query.filter_by(sender_type="team", read_by_admin=False).count()
    return render_template(
        "admin/dashboard.html",
        settings=settings,
        participant_count=participant_count,
        team_count=team_count,
        checked_in=checked_in,
        teams=teams,
        locations=LOCATIONS,
        total_unread=total_unread,
    )


# ---------------------------------------------------------------------------
# Admin: upload CSV
# ---------------------------------------------------------------------------

@app.route("/admin/upload", methods=["GET", "POST"])
@admin_required
def admin_upload():
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or not f.filename.endswith(".csv"):
            flash("Gelieve een geldig CSV-bestand te uploaden.", "danger")
            return redirect(url_for("admin_upload"))

        raw = f.stream.read().decode("utf-8-sig")
        sample = raw[:2048]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        stream = io.StringIO(raw)
        reader = csv.DictReader(stream, delimiter=delimiter)

        participants = []
        errors = []
        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
            first = row.get("voornaam") or row.get("first_name") or row.get("firstname") or row.get("naam")
            last = row.get("achternaam") or row.get("last_name") or row.get("lastname") or row.get("familienaam")
            club = row.get("leoclub") or row.get("club") or row.get("leo_club")

            if not first or not last or not club:
                errors.append(f"Rij {i}: ontbrekende kolommen ({row})")
                continue
            participants.append({"first": first, "last": last, "club": club})

        if errors:
            for e in errors:
                flash(e, "warning")

        if not participants:
            flash("Geen geldige deelnemers gevonden.", "danger")
            return redirect(url_for("admin_upload"))

        Message.query.delete()
        Participant.query.delete()
        Team.query.delete()
        LocationProgress.query.delete()
        s = get_settings()
        s.game_started = False
        s.game_started_at = None

        for p_data in participants:
            db.session.add(Participant(
                first_name=p_data["first"],
                last_name=p_data["last"],
                club=p_data["club"],
            ))
        db.session.commit()

        flash(f"✅ {len(participants)} deelnemers geladen.", "success")
        return redirect(url_for("admin_groups"))

    return render_template("admin/upload.html")


# ---------------------------------------------------------------------------
# Admin: group creation
# ---------------------------------------------------------------------------

@app.route("/admin/groups", methods=["GET", "POST"])
@admin_required
def admin_groups():
    participants = Participant.query.all()

    if request.method == "POST":
        num_groups = int(request.form.get("num_groups", 4))
        num_groups = max(2, min(num_groups, len(TEAM_COLORS)))

        for p in participants:
            p.team_id = None
            p.checked_in = False
            p.session_token = None
        Message.query.delete()
        Team.query.delete()
        LocationProgress.query.delete()
        db.session.commit()

        groups = make_balanced_groups(participants, num_groups)
        routes = generate_routes(num_groups)

        for i, (group, route) in enumerate(zip(groups, routes)):
            tc = TEAM_COLORS[i % len(TEAM_COLORS)]
            team = Team(
                name=tc["name"],
                color=tc["color"],
                bg_color=tc["bg"],
                label=tc["label"],
                route_json=json.dumps(route),
            )
            db.session.add(team)
            db.session.flush()
            for member in group:
                member.team_id = team.id

        db.session.commit()
        flash(f"✅ {num_groups} teams aangemaakt!", "success")
        return redirect(url_for("admin_groups"))

    return render_template(
        "admin/groups.html",
        participants=participants,
        teams=Team.query.all(),
        locations=LOCATIONS,
    )


# ---------------------------------------------------------------------------
# Admin: codes & settings
# ---------------------------------------------------------------------------

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    settings = get_settings()
    if request.method == "POST":
        codes = []
        for i in range(len(LOCATIONS)):
            c = request.form.get(f"code_{i}", f"CODE{i+1}").strip().upper()
            codes.append(c)
        settings.location_codes = codes
        new_pw = request.form.get("admin_password", "").strip()
        if new_pw:
            settings.admin_password = new_pw
        db.session.commit()
        flash("✅ Instellingen opgeslagen.", "success")
    return render_template("admin/settings.html", settings=settings, locations=LOCATIONS)


# ---------------------------------------------------------------------------
# Admin: QR code
# ---------------------------------------------------------------------------

@app.route("/admin/qr")
@admin_required
def admin_qr():
    ip = get_local_ip()
    port = request.environ.get("SERVER_PORT", 5050)
    base_url = f"http://{ip}:{port}"
    return render_template("admin/qr.html", base_url=base_url, ip=ip, port=port)


@app.route("/admin/qr-image")
@admin_required
def admin_qr_image():
    ip = get_local_ip()
    port = request.environ.get("SERVER_PORT", 5050)
    url = f"http://{ip}:{port}/"
    img = qrcode.make(url, box_size=12, border=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ---------------------------------------------------------------------------
# Admin: game management
# ---------------------------------------------------------------------------

@app.route("/admin/game", methods=["GET", "POST"])
@admin_required
def admin_game():
    settings = get_settings()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "start" and not settings.game_started:
            settings.game_started = True
            settings.game_started_at = datetime.utcnow()
            db.session.commit()
            flash("🚀 Spel gestart!", "success")
        elif action == "reset":
            settings.game_started = False
            settings.game_started_at = None
            Team.query.update({"current_step": 0, "finished": False, "finished_at": None})
            LocationProgress.query.delete()
            Participant.query.update({"checked_in": False, "session_token": None})
            db.session.commit()
            flash("🔄 Spel gereset.", "info")
    teams = Team.query.all()
    return render_template("admin/game.html", settings=settings, teams=teams, locations=LOCATIONS)


@app.route("/admin/game/progress")
@admin_required
def admin_game_progress():
    teams = Team.query.all()
    data = []
    for t in teams:
        loc = t.current_location
        data.append({
            "id": t.id,
            "name": t.name,
            "color": t.color,
            "step": t.current_step,
            "total": len(LOCATIONS),
            "location": loc["name"] if loc else "Klaar!",
            "finished": t.finished,
            "progress_pct": t.progress_pct,
            "checked_in_count": sum(1 for p in t.members if p.checked_in),
            "unread": t.unread_from_team,
        })
    settings = get_settings()
    return jsonify({"teams": data, "game_started": settings.game_started})


# ---------------------------------------------------------------------------
# Admin: messaging
# ---------------------------------------------------------------------------

@app.route("/admin/messages")
@admin_required
def admin_messages():
    teams = Team.query.all()
    # Mark all as read for current team if selected
    selected_team_id = request.args.get("team_id", type=int)
    selected_team = None
    conversation = []

    if selected_team_id:
        selected_team = Team.query.get(selected_team_id)
        if selected_team:
            conversation = selected_team.messages
            # Mark unread team messages as read
            Message.query.filter_by(
                team_id=selected_team_id, sender_type="team", read_by_admin=False
            ).update({"read_by_admin": True})
            db.session.commit()

    total_unread = Message.query.filter_by(sender_type="team", read_by_admin=False).count()
    return render_template(
        "admin/messages.html",
        teams=teams,
        selected_team=selected_team,
        conversation=conversation,
        total_unread=total_unread,
    )


@app.route("/admin/messages/send", methods=["POST"])
@admin_required
def admin_send_message():
    team_id = request.form.get("team_id", type=int)
    content = request.form.get("content", "").strip()
    photo_filename = save_photo(request.files.get("photo"))

    if not team_id or (not content and not photo_filename):
        flash("Bericht mag niet leeg zijn.", "warning")
        return redirect(url_for("admin_messages", team_id=team_id))

    team = Team.query.get(team_id)
    if not team:
        flash("Team niet gevonden.", "danger")
        return redirect(url_for("admin_messages"))

    msg = Message(
        team_id=team_id,
        sender_type="admin",
        sender_name="Begeleider",
        content=content,
        photo_filename=photo_filename,
        read_by_admin=True,
        read_by_team=False,
    )
    db.session.add(msg)
    db.session.commit()
    return redirect(url_for("admin_messages", team_id=team_id))


@app.route("/admin/messages/unread")
@admin_required
def admin_messages_unread():
    teams = Team.query.all()
    by_team = {str(t.id): t.unread_from_team for t in teams}
    team_names = {str(t.id): t.name for t in teams}
    total = sum(by_team.values())
    return jsonify({"by_team": by_team, "team_names": team_names, "total": total})


# ---------------------------------------------------------------------------
# Participant: landing
# ---------------------------------------------------------------------------

@app.route("/")
def participant_index():
    token = session.get("participant_token")
    participant = None
    if token:
        participant = Participant.query.filter_by(session_token=token).first()
        if participant and participant.checked_in:
            return redirect(url_for("participant_game"))
    has_participants = Participant.query.count() > 0
    return render_template(
        "participant/index.html",
        has_participants=has_participants,
        participant=participant,
    )


@app.route("/join", methods=["GET", "POST"])
def participant_join():
    if request.method == "POST":
        participant_id = request.form.get("participant_id", type=int)
        p = Participant.query.get(participant_id)

        if not p:
            return jsonify({"success": False, "message": "Deelnemer niet gevonden."}), 404

        if p.checked_in and p.session_token:
            # Already checked in – let them back in on this device
            session["participant_token"] = p.session_token
            return jsonify({"success": True, "redirect": url_for("participant_game")})

        if not p.team_id:
            return jsonify({
                "success": False,
                "message": "Je bent nog niet toegewezen aan een team. Vraag de begeleider om teams aan te maken."
            })

        token = os.urandom(16).hex()
        p.session_token = token
        p.checked_in = True
        db.session.commit()

        session["participant_token"] = token
        return jsonify({"success": True, "redirect": url_for("participant_game")})

    # GET – return participant list grouped by club
    participants = Participant.query.order_by(Participant.club, Participant.last_name).all()
    teams = {t.id: t for t in Team.query.all()}
    has_teams = len(teams) > 0

    # Group by club
    by_club = {}
    for p in participants:
        by_club.setdefault(p.club, []).append(p)

    return render_template(
        "participant/join.html",
        by_club=by_club,
        teams=teams,
        has_teams=has_teams,
    )


@app.route("/join/info/<int:participant_id>")
def participant_info(participant_id):
    """JSON: returns team info for a participant (used by JS before confirming)."""
    p = Participant.query.get_or_404(participant_id)
    if not p.team:
        return jsonify({"has_team": False})
    return jsonify({
        "has_team": True,
        "team_name": p.team.name,
        "team_color": p.team.color,
        "team_bg": p.team.bg_color,
        "already_checked_in": p.checked_in,
    })


# ---------------------------------------------------------------------------
# Participant: game
# ---------------------------------------------------------------------------

@app.route("/game")
def participant_game():
    token = session.get("participant_token")
    if not token:
        return redirect(url_for("participant_index"))
    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return redirect(url_for("participant_index"))

    settings = get_settings()
    team = p.team
    return render_template(
        "participant/game.html",
        participant=p,
        team=team,
        settings=settings,
        locations=LOCATIONS,
        finish_message=FINISH_MESSAGE,
    )


@app.route("/game/status")
def participant_game_status():
    token = session.get("participant_token")
    if not token:
        return jsonify({"error": "not_logged_in"}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return jsonify({"error": "no_team"}), 400

    settings = get_settings()
    team = p.team
    loc = team.current_location
    unread = Message.query.filter_by(
        team_id=team.id, sender_type="admin", read_by_team=False
    ).count()

    return jsonify({
        "game_started": settings.game_started,
        "current_step": team.current_step,
        "total_steps": len(LOCATIONS),
        "finished": team.finished,
        "location_name": loc["name"] if loc else None,
        "location_emoji": loc["emoji"] if loc else None,
        "clue": loc["clue"] if loc else None,
        "challenge": loc["challenge"] if loc else None,
        "arrival_hint": loc["arrival_hint"] if loc else None,
        "progress_pct": team.progress_pct,
        "unread_messages": unread,
    })


@app.route("/game/submit-code", methods=["POST"])
def participant_submit_code():
    token = session.get("participant_token")
    if not token:
        return jsonify({"success": False, "message": "Niet ingelogd."}), 401

    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return jsonify({"success": False, "message": "Geen team gevonden."}), 400

    settings = get_settings()
    if not settings.game_started:
        return jsonify({"success": False, "message": "Het spel is nog niet gestart."}), 400

    team = p.team
    if team.finished:
        return jsonify({"success": True, "finished": True, "message": FINISH_MESSAGE})

    code_input = request.json.get("code", "").strip().upper()
    loc = team.current_location
    if not loc:
        return jsonify({"success": False, "message": "Geen locatie gevonden."}), 400

    expected = settings.location_codes[loc["id"]].strip().upper()
    if code_input != expected:
        return jsonify({"success": False, "message": "Verkeerde code. Probeer opnieuw!"}), 200

    prog = LocationProgress(
        team_id=team.id,
        location_id=loc["id"],
        arrived_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
    db.session.add(prog)

    team.current_step += 1
    if team.current_step >= len(LOCATIONS):
        team.finished = True
        team.finished_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "finished": True, "message": FINISH_MESSAGE})

    next_loc = team.current_location
    db.session.commit()
    return jsonify({
        "success": True,
        "finished": False,
        "next_location": next_loc["name"],
        "next_clue": next_loc["clue"],
        "next_emoji": next_loc["emoji"],
        "message": f"✅ Goed gedaan! Jullie volgende locatie is: {next_loc['name']}",
    })


# ---------------------------------------------------------------------------
# Participant: messaging
# ---------------------------------------------------------------------------

@app.route("/game/messages")
def participant_get_messages():
    token = session.get("participant_token")
    if not token:
        return jsonify({"error": "not_logged_in"}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return jsonify({"error": "no_team"}), 400

    msgs = Message.query.filter_by(team_id=p.team_id).order_by(Message.sent_at).all()
    # Mark admin messages as read
    Message.query.filter_by(
        team_id=p.team_id, sender_type="admin", read_by_team=False
    ).update({"read_by_team": True})
    db.session.commit()

    return jsonify({"messages": [m.to_dict() for m in msgs]})


@app.route("/game/messages/send", methods=["POST"])
def participant_send_message():
    token = session.get("participant_token")
    if not token:
        return jsonify({"success": False, "message": "Niet ingelogd."}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return jsonify({"success": False, "message": "Geen team."}), 400

    content = request.form.get("content", "").strip()
    photo_filename = save_photo(request.files.get("photo"))

    if not content and not photo_filename:
        return jsonify({"success": False, "message": "Leeg bericht."}), 400

    msg = Message(
        team_id=p.team_id,
        sender_type="team",
        sender_name=p.full_name,
        content=content,
        photo_filename=photo_filename,
        read_by_admin=False,
        read_by_team=True,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "message": msg.to_dict()})


# ---------------------------------------------------------------------------
# Participant: logout
# ---------------------------------------------------------------------------

@app.route("/game/logout")
def participant_logout():
    token = session.pop("participant_token", None)
    if token:
        p = Participant.query.filter_by(session_token=token).first()
        if p:
            p.checked_in = False
            p.session_token = None
            db.session.commit()
    return redirect(url_for("participant_index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)

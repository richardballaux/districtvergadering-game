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

from game_content import FINISH_MESSAGE, LOCATIONS as DEFAULT_LOCATIONS, TEAM_COLORS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "districtvergadering-leos-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///game.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(100), nullable=False)
    emoji = db.Column(db.String(10), nullable=False, default="📍")
    clue = db.Column(db.Text, nullable=False, default="")
    challenge = db.Column(db.Text, nullable=False, default="")
    arrival_hint = db.Column(db.String(500), nullable=True)
    answer_type = db.Column(db.String(10), nullable=False, default="code")  # 'code' or 'photo'
    secret_code = db.Column(db.String(50), nullable=True)
    challenge_image = db.Column(db.String(200), nullable=True)  # optional image shown with the challenge
    submissions = db.relationship("PhotoSubmission", backref="location", lazy=True)


class PhotoSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False)
    participant_name = db.Column(db.String(100), nullable=False)
    photo_filename = db.Column(db.String(200), nullable=False)
    caption = db.Column(db.Text, nullable=True)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default="pending")  # pending | approved | rejected
    feedback = db.Column(db.Text, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    @property
    def photo_url(self):
        return url_for("uploaded_file", filename=self.photo_filename)


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
        return f"{self.first_name} {self.last_name}".strip()


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    color = db.Column(db.String(20), nullable=False)
    bg_color = db.Column(db.String(20), nullable=False)
    label = db.Column(db.String(20), nullable=False)
    current_step = db.Column(db.Integer, default=0)
    route_json = db.Column(db.String(200), nullable=False, default="[]")
    finished = db.Column(db.Boolean, default=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    members = db.relationship("Participant", backref="team", lazy=True)
    messages = db.relationship("Message", backref="team", lazy=True,
                               order_by="Message.sent_at")
    photo_submissions = db.relationship("PhotoSubmission", backref="team", lazy=True)

    @property
    def route(self):
        return json.loads(self.route_json)

    @property
    def total_steps(self):
        return len(self.route)

    @property
    def current_location(self):
        r = self.route
        if self.current_step < len(r):
            return Location.query.get(r[self.current_step])
        return None

    @property
    def progress_pct(self):
        total = self.total_steps
        if total == 0:
            return 0
        return int((self.current_step / total) * 100)

    @property
    def unread_from_team(self):
        return Message.query.filter_by(
            team_id=self.id, sender_type="team", read_by_admin=False
        ).count()

    def current_submission(self, location_id):
        """Latest photo submission for a given location."""
        return (PhotoSubmission.query
                .filter_by(team_id=self.id, location_id=location_id)
                .order_by(PhotoSubmission.submitted_at.desc())
                .first())


class VotingPhoto(db.Model):
    """A photo selected by the admin for the voting slideshow."""
    id = db.Column(db.Integer, primary_key=True)
    photo_filename = db.Column(db.String(200), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=True)
    participant_name = db.Column(db.String(100), nullable=True)
    source_label = db.Column(db.String(200), nullable=True)
    order_index = db.Column(db.Integer, default=0)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    votes = db.relationship("Vote", backref="photo", lazy=True,
                            cascade="all, delete-orphan")

    @property
    def vote_count(self):
        return len(self.votes)


class VotingSession(db.Model):
    """Singleton that tracks the voting slideshow state."""
    id = db.Column(db.Integer, primary_key=True)
    # setup | preview | voting | finished
    status = db.Column(db.String(20), default="setup")
    current_photo_id = db.Column(db.Integer, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("participant.id"),
                               nullable=False)
    voting_photo_id = db.Column(db.Integer, db.ForeignKey("voting_photo.id"),
                                nullable=False)
    voted_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("participant_id", "voting_photo_id",
                            name="uq_participant_vote"),
    )


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
    admin_password = db.Column(db.String(100), default="RikkertLeos1")
    num_stops = db.Column(db.Integer, default=3)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    sender_type = db.Column(db.String(10), nullable=False)
    sender_name = db.Column(db.String(100), nullable=True)
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


MAX_VOTES = 3  # votes each participant gets


def get_voting_session():
    s = VotingSession.query.first()
    if not s:
        s = VotingSession(status="setup")
        db.session.add(s)
        db.session.commit()
    return s


def get_ordered_voting_photos():
    return VotingPhoto.query.order_by(VotingPhoto.order_index).all()


def get_all_gallery_photos():
    """Collect all team-sent photos (messages + submissions) for the gallery."""
    result = []
    # Team message photos
    for msg in (Message.query
                .filter_by(sender_type="team")
                .filter(Message.photo_filename.isnot(None))
                .order_by(Message.sent_at).all()):
        team = Team.query.get(msg.team_id)
        result.append({
            "filename": msg.photo_filename,
            "team_id": msg.team_id,
            "team_name": team.name if team else "?",
            "team_color": team.color if team else "#aaa",
            "participant_name": msg.sender_name or (team.name if team else "?"),
            "source_label": f"Bericht – {team.name if team else '?'}",
            "key": f"msg_{msg.id}",
        })
    # Submission photos (all statuses)
    for sub in PhotoSubmission.query.order_by(PhotoSubmission.submitted_at).all():
        team = Team.query.get(sub.team_id)
        loc = Location.query.get(sub.location_id)
        result.append({
            "filename": sub.photo_filename,
            "team_id": sub.team_id,
            "team_name": team.name if team else "?",
            "team_color": team.color if team else "#aaa",
            "participant_name": sub.participant_name,
            "source_label": f"Opdracht {loc.name if loc else '?'} – {team.name if team else '?'}",
            "key": f"sub_{sub.id}",
        })
    return result


def voting_status_dict(vs, vp_list, for_team_id=None):
    """JSON-serialisable snapshot of the voting session."""
    cur = VotingPhoto.query.get(vs.current_photo_id) if vs.current_photo_id else None
    idx = next((i for i, vp in enumerate(vp_list) if vp.id == vs.current_photo_id), None)
    result = {
        "status": vs.status,
        "total": len(vp_list),
        "current_index": (idx + 1) if idx is not None else 0,
        "current": None,
        "results": [],
    }
    if cur:
        can_vote = (
            vs.status == "voting"
            and for_team_id is not None
            and cur.team_id != for_team_id
        )
        result["current"] = {
            "id": cur.id,
            "photo_url": url_for("uploaded_file", filename=cur.photo_filename),
            "participant_name": cur.participant_name,
            "source_label": cur.source_label,
            "team_id": cur.team_id,
            "vote_count": cur.vote_count,
            "can_vote_if_eligible": can_vote,
        }
    if vs.status == "finished":
        result["results"] = [
            {
                "id": vp.id,
                "photo_url": url_for("uploaded_file", filename=vp.photo_filename),
                "participant_name": vp.participant_name,
                "source_label": vp.source_label,
                "vote_count": vp.vote_count,
            }
            for vp in sorted(vp_list, key=lambda v: v.vote_count, reverse=True)
        ]
    return result


def get_locations():
    return Location.query.order_by(Location.order_index).all()


def seed_locations():
    """Seed default locations from game_content.py if DB is empty."""
    if Location.query.count() == 0:
        for i, loc in enumerate(DEFAULT_LOCATIONS):
            db.session.add(Location(
                order_index=i,
                name=loc["name"],
                emoji=loc["emoji"],
                clue=loc["clue"],
                challenge=loc["challenge"],
                arrival_hint=loc.get("arrival_hint", ""),
                answer_type="code",
                secret_code=f"CODE{i + 1}",
            ))
        db.session.commit()


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


def generate_routes(num_teams, num_stops=None):
    """Geeft elk team een willekeurige volgorde van num_stops locaties.

    Als num_stops >= aantal locaties krijgt elk team alle locaties in een
    willekeurige volgorde. Anders krijgt elk team een willekeurige subset
    van num_stops locaties, ook in willekeurige volgorde.
    """
    import random
    locations = get_locations()
    base = [loc.id for loc in locations]
    if not base:
        return [[] for _ in range(num_teams)]
    if num_stops is None or num_stops <= 0:
        num_stops = len(base)
    num_stops = min(num_stops, len(base))

    routes = []
    for _ in range(num_teams):
        shuffled = base[:]
        random.shuffle(shuffled)
        routes.append(shuffled[:num_stops])
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


def pending_submissions_count():
    return PhotoSubmission.query.filter_by(status="pending").count()


# ---------------------------------------------------------------------------
# Init DB
# ---------------------------------------------------------------------------

def migrate_columns():
    """Add any missing columns that were added after initial DB creation."""
    with db.engine.connect() as conn:
        # location.challenge_image
        loc_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(location)"))]
        if "challenge_image" not in loc_cols:
            conn.execute(db.text("ALTER TABLE location ADD COLUMN challenge_image VARCHAR(200)"))
            conn.commit()
        # game_settings.num_stops
        gs_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(game_settings)"))]
        if "num_stops" not in gs_cols:
            conn.execute(db.text("ALTER TABLE game_settings ADD COLUMN num_stops INTEGER DEFAULT 3"))
            conn.commit()


def migrate_routes():
    """Convert old 0-indexed routes to DB-ID routes if needed."""
    locations = Location.query.order_by(Location.order_index).all()
    if not locations:
        return
    id_list = [loc.id for loc in locations]
    changed = False
    for team in Team.query.all():
        route = team.route
        # Old routes used 0-based indices; DB IDs start at 1, so 0 in a route means old style
        if route and any(r == 0 or r not in id_list for r in route):
            new_route = [id_list[r % len(id_list)] for r in route]
            team.route_json = json.dumps(new_route)
            changed = True
    if changed:
        db.session.commit()


with app.app_context():
    db.create_all()
    migrate_columns()
    get_settings()
    seed_locations()
    migrate_routes()
    get_voting_session()


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
        if pw == get_settings().admin_password:
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
    locations = get_locations()
    teams = Team.query.all()
    return render_template(
        "admin/dashboard.html",
        settings=get_settings(),
        participant_count=Participant.query.count(),
        team_count=len(teams),
        checked_in=Participant.query.filter_by(checked_in=True).count(),
        teams=teams,
        locations=locations,
        locations_by_id={loc.id: loc for loc in locations},
        total_unread=Message.query.filter_by(sender_type="team", read_by_admin=False).count(),
        pending_submissions=pending_submissions_count(),
    )


# ---------------------------------------------------------------------------
# Admin: upload CSV / Excel
# ---------------------------------------------------------------------------

def _parse_xlsx(stream):
    """Leest een Google Forms Excel export. Geeft lijst van (full_name, club) of foutboodschap."""
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(stream.read()))
    ws = wb.active

    # Lees headers uit rij 1 (case-insensitive, gestript)
    headers = []
    for cell in ws[1]:
        headers.append((cell.value or "").strip().lower())

    # Zoek de juiste kolommen op naam
    NAME_VARIANTS = {"naam & voornaam", "naam en voornaam", "naam &voornaam",
                     "naam&voornaam", "naam", "voornaam", "name"}
    CLUB_VARIANTS = {"leo club", "leoclub", "leo_club", "club"}

    name_col = next((i for i, h in enumerate(headers) if h in NAME_VARIANTS), None)
    club_col = next((i for i, h in enumerate(headers) if h in CLUB_VARIANTS), None)

    if name_col is None:
        return None, "Kolom 'Naam & Voornaam' niet gevonden. Gevonden koppen: " + ", ".join(headers)
    if club_col is None:
        return None, "Kolom 'Leo Club' niet gevonden. Gevonden koppen: " + ", ".join(headers)

    participants, errors = [], []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        name = str(row[name_col]).strip() if row[name_col] else ""
        club = str(row[club_col]).strip() if row[club_col] else ""
        if not name or name.lower() == "none":
            errors.append(f"Rij {i}: lege naam, overgeslagen.")
            continue
        if not club or club.lower() == "none":
            errors.append(f"Rij {i}: '{name}' heeft geen club, overgeslagen.")
            continue
        participants.append({"full_name": name, "club": club})

    return participants, errors


def _parse_csv(stream):
    """Leest een CSV-bestand met voornaam/achternaam of naam + club kolommen."""
    raw = stream.read().decode("utf-8-sig")
    sample = raw[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)

    participants, errors = [], []
    for i, row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}

        # Support "Naam & Voornaam" als één kolom (Google Forms CSV export)
        full_name_raw = (
            row.get("naam & voornaam") or row.get("naam &voornaam") or
            row.get("naam en voornaam") or row.get("naam&voornaam")
        )
        if full_name_raw:
            full_name = full_name_raw
        else:
            first = (row.get("voornaam") or row.get("first_name") or
                     row.get("firstname") or row.get("naam") or "")
            last = (row.get("achternaam") or row.get("last_name") or
                    row.get("lastname") or row.get("familienaam") or "")
            full_name = f"{first} {last}".strip()

        club = (row.get("leo club") or row.get("leoclub") or
                row.get("club") or row.get("leo_club") or "")

        if not full_name:
            errors.append(f"Rij {i}: lege naam, overgeslagen.")
            continue
        if not club:
            errors.append(f"Rij {i}: '{full_name}' heeft geen club, overgeslagen.")
            continue
        participants.append({"full_name": full_name, "club": club})

    return participants, errors


@app.route("/admin/upload", methods=["GET", "POST"])
@admin_required
def admin_upload():
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or not f.filename:
            flash("Geen bestand geselecteerd.", "danger")
            return redirect(url_for("admin_upload"))

        fname = f.filename.lower()
        if fname.endswith(".xlsx"):
            participants, errors = _parse_xlsx(f.stream)
            if participants is None:
                flash(f"❌ {errors}", "danger")
                return redirect(url_for("admin_upload"))
        elif fname.endswith(".csv"):
            participants, errors = _parse_csv(f.stream)
        else:
            flash("Gelieve een .xlsx of .csv bestand te uploaden.", "danger")
            return redirect(url_for("admin_upload"))

        for e in errors:
            flash(e, "warning")
        if not participants:
            flash("Geen geldige deelnemers gevonden.", "danger")
            return redirect(url_for("admin_upload"))

        # Reset game state
        Message.query.delete()
        PhotoSubmission.query.delete()
        Participant.query.delete()
        Team.query.delete()
        LocationProgress.query.delete()
        s = get_settings()
        s.game_started = False
        s.game_started_at = None
        for p in participants:
            db.session.add(Participant(
                first_name=p["full_name"],
                last_name="",
                club=p["club"],
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
    locations = get_locations()

    if request.method == "POST":
        num_groups = int(request.form.get("num_groups", 4))
        num_groups = max(2, min(num_groups, len(TEAM_COLORS)))
        for p in participants:
            p.team_id = None
            p.checked_in = False
            p.session_token = None
        Message.query.delete()
        PhotoSubmission.query.delete()
        Team.query.delete()
        LocationProgress.query.delete()
        db.session.commit()

        groups = make_balanced_groups(participants, num_groups)
        routes = generate_routes(num_groups, num_stops=get_settings().num_stops)
        for i, (group, route) in enumerate(zip(groups, routes)):
            tc = TEAM_COLORS[i % len(TEAM_COLORS)]
            team = Team(
                name=tc["name"], color=tc["color"], bg_color=tc["bg"],
                label=tc["label"], route_json=json.dumps(route),
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
        locations=locations,
        locations_by_id={loc.id: loc for loc in locations},
    )


# ---------------------------------------------------------------------------
# Admin: opdrachten (CRUD voor locaties)
# ---------------------------------------------------------------------------

@app.route("/admin/opdrachten")
@admin_required
def admin_opdrachten():
    locations = get_locations()
    return render_template("admin/opdrachten.html", locations=locations)


@app.route("/admin/opdrachten/add", methods=["POST"])
@admin_required
def admin_opdrachten_add():
    max_order = db.session.query(db.func.max(Location.order_index)).scalar() or -1
    challenge_image = save_photo(request.files.get("challenge_image"))
    loc = Location(
        order_index=max_order + 1,
        name=request.form.get("name", "Nieuwe locatie"),
        emoji=request.form.get("emoji", "📍"),
        clue=request.form.get("clue", ""),
        challenge=request.form.get("challenge", ""),
        arrival_hint=request.form.get("arrival_hint", ""),
        answer_type=request.form.get("answer_type", "code"),
        secret_code=request.form.get("secret_code", "").strip().upper() or None,
        challenge_image=challenge_image,
    )
    db.session.add(loc)
    db.session.commit()
    flash(f"✅ Locatie '{loc.name}' toegevoegd.", "success")
    return redirect(url_for("admin_opdrachten"))


@app.route("/admin/opdrachten/<int:loc_id>/edit", methods=["POST"])
@admin_required
def admin_opdrachten_edit(loc_id):
    loc = Location.query.get_or_404(loc_id)
    loc.name = request.form.get("name", loc.name)
    loc.emoji = request.form.get("emoji", loc.emoji)
    loc.clue = request.form.get("clue", loc.clue)
    loc.challenge = request.form.get("challenge", loc.challenge)
    loc.arrival_hint = request.form.get("arrival_hint", loc.arrival_hint)
    loc.answer_type = request.form.get("answer_type", loc.answer_type)
    loc.secret_code = request.form.get("secret_code", "").strip().upper() or None
    # Challenge image: new upload takes priority; checkbox to delete existing
    new_image = save_photo(request.files.get("challenge_image"))
    if new_image:
        loc.challenge_image = new_image
    elif request.form.get("delete_challenge_image"):
        loc.challenge_image = None
    db.session.commit()
    flash(f"✅ '{loc.name}' opgeslagen.", "success")
    return redirect(url_for("admin_opdrachten"))


@app.route("/admin/opdrachten/<int:loc_id>/delete", methods=["POST"])
@admin_required
def admin_opdrachten_delete(loc_id):
    if get_settings().game_started:
        flash("Kan geen locatie verwijderen terwijl het spel actief is.", "danger")
        return redirect(url_for("admin_opdrachten"))
    loc = Location.query.get_or_404(loc_id)
    db.session.delete(loc)
    # Re-index remaining
    for i, l in enumerate(get_locations()):
        l.order_index = i
    db.session.commit()
    flash(f"🗑️ '{loc.name}' verwijderd.", "info")
    return redirect(url_for("admin_opdrachten"))


@app.route("/admin/opdrachten/<int:loc_id>/move", methods=["POST"])
@admin_required
def admin_opdrachten_move(loc_id):
    direction = request.form.get("direction")  # 'up' or 'down'
    loc = Location.query.get_or_404(loc_id)
    locations = get_locations()
    idx = next((i for i, l in enumerate(locations) if l.id == loc_id), None)
    if idx is None:
        return redirect(url_for("admin_opdrachten"))
    if direction == "up" and idx > 0:
        locations[idx].order_index, locations[idx - 1].order_index = \
            locations[idx - 1].order_index, locations[idx].order_index
    elif direction == "down" and idx < len(locations) - 1:
        locations[idx].order_index, locations[idx + 1].order_index = \
            locations[idx + 1].order_index, locations[idx].order_index
    db.session.commit()
    return redirect(url_for("admin_opdrachten"))


# ---------------------------------------------------------------------------
# Admin: inzendingen (photo submission review)
# ---------------------------------------------------------------------------

@app.route("/admin/inzendingen")
@admin_required
def admin_inzendingen():
    filter_status = request.args.get("status", "pending")
    query = PhotoSubmission.query
    if filter_status != "all":
        query = query.filter_by(status=filter_status)
    submissions = query.order_by(PhotoSubmission.submitted_at.desc()).all()
    teams = {t.id: t for t in Team.query.all()}
    locations = {loc.id: loc for loc in Location.query.all()}
    return render_template(
        "admin/inzendingen.html",
        submissions=submissions,
        teams=teams,
        locations=locations,
        filter_status=filter_status,
        pending_count=pending_submissions_count(),
    )


@app.route("/admin/inzendingen/<int:sub_id>/approve", methods=["POST"])
@admin_required
def admin_approve_submission(sub_id):
    sub = PhotoSubmission.query.get_or_404(sub_id)
    if sub.status != "pending":
        flash("Inzending al beoordeeld.", "warning")
        return redirect(url_for("admin_inzendingen"))

    sub.status = "approved"
    sub.reviewed_at = datetime.utcnow()

    # Advance team to next step
    team = Team.query.get(sub.team_id)
    if team and not team.finished:
        loc = team.current_location
        if loc and loc.id == sub.location_id:
            # Record progress
            db.session.add(LocationProgress(
                team_id=team.id, location_id=loc.id,
                arrived_at=sub.submitted_at, completed_at=datetime.utcnow(),
            ))
            team.current_step += 1
            if team.current_step >= team.total_steps:
                team.finished = True
                team.finished_at = datetime.utcnow()

    db.session.commit()
    flash(f"✅ Inzending van {sub.participant_name} goedgekeurd.", "success")
    return redirect(url_for("admin_inzendingen"))


@app.route("/admin/inzendingen/<int:sub_id>/reject", methods=["POST"])
@admin_required
def admin_reject_submission(sub_id):
    sub = PhotoSubmission.query.get_or_404(sub_id)
    feedback = request.form.get("feedback", "").strip()
    if not feedback:
        flash("Geef feedback mee bij afkeuring.", "warning")
        return redirect(url_for("admin_inzendingen"))
    sub.status = "rejected"
    sub.feedback = feedback
    sub.reviewed_at = datetime.utcnow()
    db.session.commit()
    flash(f"❌ Inzending afgekeurd. Feedback verstuurd naar {sub.team.name if sub.team else '?'}.", "info")
    return redirect(url_for("admin_inzendingen"))


@app.route("/admin/inzendingen/count")
@admin_required
def admin_submissions_count():
    return jsonify({"pending": pending_submissions_count()})


# ---------------------------------------------------------------------------
# Admin: settings (password only)
# ---------------------------------------------------------------------------

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    settings = get_settings()
    locations = get_locations()
    if request.method == "POST":
        changed = False
        new_pw = request.form.get("admin_password", "").strip()
        if new_pw:
            settings.admin_password = new_pw
            changed = True

        try:
            ns = int(request.form.get("num_stops", settings.num_stops))
            ns = max(1, min(ns, len(locations)))
            if ns != settings.num_stops:
                settings.num_stops = ns
                changed = True
        except (ValueError, TypeError):
            flash("Ongeldig aantal stops.", "warning")

        if changed:
            db.session.commit()
            flash("✅ Instellingen opgeslagen.", "success")
    return render_template("admin/settings.html", settings=settings, locations=locations)


# ---------------------------------------------------------------------------
# Admin: QR code
# ---------------------------------------------------------------------------

PUBLIC_URL = os.environ.get(
    "PUBLIC_URL",
    "https://districtvergadering-game-cy3q.onrender.com"
).rstrip("/")


@app.route("/admin/qr")
@admin_required
def admin_qr():
    return render_template("admin/qr.html", public_url=PUBLIC_URL)


@app.route("/admin/qr/beamer")
@admin_required
def admin_qr_beamer():
    """Standalone fullscreen beamer page — no sidebar."""
    return render_template("admin/qr_beamer.html", public_url=PUBLIC_URL)


@app.route("/admin/qr-image")
@admin_required
def admin_qr_image():
    img = qrcode.make(PUBLIC_URL + "/", box_size=12, border=4)
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
            PhotoSubmission.query.delete()
            Participant.query.update({"checked_in": False, "session_token": None})
            db.session.commit()
            flash("🔄 Spel gereset.", "info")
    locations = get_locations()
    return render_template(
        "admin/game.html",
        settings=settings,
        teams=Team.query.all(),
        locations=locations,
        locations_by_id={loc.id: loc for loc in locations},
    )


@app.route("/admin/game/progress")
@admin_required
def admin_game_progress():
    teams = Team.query.all()
    settings = get_settings()
    data = []
    for t in teams:
        loc = t.current_location
        data.append({
            "id": t.id, "name": t.name, "color": t.color,
            "step": t.current_step, "total": t.total_steps,
            "location": loc.name if loc else "Klaar!",
            "finished": t.finished, "progress_pct": t.progress_pct,
            "checked_in_count": sum(1 for p in t.members if p.checked_in),
            "unread": t.unread_from_team,
        })
    return jsonify({"teams": data, "game_started": settings.game_started})


# ---------------------------------------------------------------------------
# Admin: messaging
# ---------------------------------------------------------------------------

@app.route("/admin/messages")
@admin_required
def admin_messages():
    teams = Team.query.all()
    selected_team_id = request.args.get("team_id", type=int)
    selected_team = None
    conversation = []
    if selected_team_id:
        selected_team = Team.query.get(selected_team_id)
        if selected_team:
            conversation = selected_team.messages
            Message.query.filter_by(
                team_id=selected_team_id, sender_type="team", read_by_admin=False
            ).update({"read_by_admin": True})
            db.session.commit()
    return render_template(
        "admin/messages.html",
        teams=teams, selected_team=selected_team, conversation=conversation,
        total_unread=Message.query.filter_by(sender_type="team", read_by_admin=False).count(),
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
    if not Team.query.get(team_id):
        flash("Team niet gevonden.", "danger")
        return redirect(url_for("admin_messages"))
    db.session.add(Message(
        team_id=team_id, sender_type="admin", sender_name="Begeleider",
        content=content, photo_filename=photo_filename,
        read_by_admin=True, read_by_team=False,
    ))
    db.session.commit()
    return redirect(url_for("admin_messages", team_id=team_id))


@app.route("/admin/messages/unread")
@admin_required
def admin_messages_unread():
    teams = Team.query.all()
    by_team = {str(t.id): t.unread_from_team for t in teams}
    team_names = {str(t.id): t.name for t in teams}
    return jsonify({
        "by_team": by_team, "team_names": team_names,
        "total": sum(by_team.values()),
        "pending_submissions": pending_submissions_count(),
    })


# ---------------------------------------------------------------------------
# Participant: landing
# ---------------------------------------------------------------------------

@app.route("/")
def participant_index():
    token = session.get("participant_token")
    if token:
        p = Participant.query.filter_by(session_token=token).first()
        if p and p.checked_in:
            return redirect(url_for("participant_game"))
    return render_template(
        "participant/index.html",
        has_participants=Participant.query.count() > 0,
    )


@app.route("/join", methods=["GET", "POST"])
def participant_join():
    if request.method == "POST":
        p = Participant.query.get(request.form.get("participant_id", type=int))
        if not p:
            return jsonify({"success": False, "message": "Deelnemer niet gevonden."}), 404
        if p.checked_in and p.session_token:
            session["participant_token"] = p.session_token
            return jsonify({"success": True, "redirect": url_for("participant_game")})
        if not p.team_id:
            return jsonify({"success": False,
                            "message": "Nog niet ingedeeld in een team. Vraag de begeleider."})
        token = os.urandom(16).hex()
        p.session_token = token
        p.checked_in = True
        db.session.commit()
        session["participant_token"] = token
        return jsonify({"success": True, "redirect": url_for("participant_game")})

    participants = Participant.query.order_by(Participant.club, Participant.last_name).all()
    teams = {t.id: t for t in Team.query.all()}
    by_club = {}
    for p in participants:
        by_club.setdefault(p.club, []).append(p)
    return render_template("participant/join.html",
                           by_club=by_club, teams=teams, has_teams=bool(teams))


@app.route("/join/info/<int:participant_id>")
def participant_info(participant_id):
    p = Participant.query.get_or_404(participant_id)
    if not p.team:
        return jsonify({"has_team": False})
    return jsonify({
        "has_team": True, "team_name": p.team.name,
        "team_color": p.team.color, "team_bg": p.team.bg_color,
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
    return render_template(
        "participant/game.html",
        participant=p, team=p.team,
        settings=get_settings(),
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

    submission_status = None
    submission_feedback = None
    if loc and loc.answer_type == "photo":
        sub = team.current_submission(loc.id)
        if sub:
            submission_status = sub.status
            submission_feedback = sub.feedback

    unread = Message.query.filter_by(
        team_id=team.id, sender_type="admin", read_by_team=False
    ).count()

    vs = get_voting_session()

    return jsonify({
        "game_started": settings.game_started,
        "current_step": team.current_step,
        "total_steps": team.total_steps,
        "finished": team.finished,
        "location_name": loc.name if loc else None,
        "location_emoji": loc.emoji if loc else None,
        "clue": loc.clue if loc else None,
        "challenge": loc.challenge if loc else None,
        "arrival_hint": loc.arrival_hint if loc else None,
        "answer_type": loc.answer_type if loc else "code",
        "challenge_image": url_for("uploaded_file", filename=loc.challenge_image)
                           if loc and loc.challenge_image else None,
        "progress_pct": team.progress_pct,
        "submission_status": submission_status,
        "submission_feedback": submission_feedback,
        "unread_messages": unread,
        "voting_status": vs.status,
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

    loc = team.current_location
    if not loc or loc.answer_type != "code":
        return jsonify({"success": False, "message": "Deze locatie verwacht geen code."}), 400

    code_input = (request.json or {}).get("code", "").strip().upper()
    expected = (loc.secret_code or "").strip().upper()
    if not expected:
        return jsonify({"success": False, "message": "Geen code ingesteld voor deze locatie."}), 400
    if code_input != expected:
        return jsonify({"success": False, "message": "Verkeerde code. Probeer opnieuw!"}), 200

    db.session.add(LocationProgress(
        team_id=team.id, location_id=loc.id,
        arrived_at=datetime.utcnow(), completed_at=datetime.utcnow(),
    ))
    team.current_step += 1
    if team.current_step >= team.total_steps:
        team.finished = True
        team.finished_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "finished": True, "message": FINISH_MESSAGE})

    next_loc = team.current_location
    db.session.commit()
    return jsonify({
        "success": True, "finished": False,
        "next_location": next_loc.name, "next_clue": next_loc.clue,
        "next_emoji": next_loc.emoji,
        "message": f"✅ Goed gedaan! Jullie volgende locatie is: {next_loc.name}",
    })


@app.route("/game/submit-photo", methods=["POST"])
def participant_submit_photo():
    token = session.get("participant_token")
    if not token:
        return jsonify({"success": False, "message": "Niet ingelogd."}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p or not p.team:
        return jsonify({"success": False, "message": "Geen team gevonden."}), 400
    if not get_settings().game_started:
        return jsonify({"success": False, "message": "Het spel is nog niet gestart."}), 400

    team = p.team
    loc = team.current_location
    if not loc or loc.answer_type != "photo":
        return jsonify({"success": False, "message": "Deze locatie verwacht geen foto."}), 400

    # Check if already pending/approved
    existing = team.current_submission(loc.id)
    if existing and existing.status == "pending":
        return jsonify({"success": False, "message": "Jullie foto is al ingediend en wordt beoordeeld."}), 400
    if existing and existing.status == "approved":
        return jsonify({"success": False, "message": "Jullie opdracht is al goedgekeurd."}), 400

    photo_filename = save_photo(request.files.get("photo"))
    if not photo_filename:
        return jsonify({"success": False, "message": "Geen geldige foto ontvangen."}), 400

    caption = request.form.get("caption", "").strip()
    db.session.add(PhotoSubmission(
        team_id=team.id, location_id=loc.id,
        participant_name=p.full_name, photo_filename=photo_filename,
        caption=caption, status="pending",
    ))
    db.session.commit()
    return jsonify({"success": True, "message": "📸 Foto ingediend! Wachten op goedkeuring van de begeleider."})


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
        team_id=p.team_id, sender_type="team", sender_name=p.full_name,
        content=content, photo_filename=photo_filename,
        read_by_admin=False, read_by_team=True,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "message": msg.to_dict()})


# ---------------------------------------------------------------------------
# Admin: foto-galerij
# ---------------------------------------------------------------------------

@app.route("/admin/galerij")
@admin_required
def admin_galerij():
    gallery = get_all_gallery_photos()
    voting_photos = get_ordered_voting_photos()
    in_voting = {vp.photo_filename: vp for vp in voting_photos}
    return render_template(
        "admin/galerij.html",
        gallery=gallery,
        voting_photos=voting_photos,
        in_voting=in_voting,
    )


@app.route("/admin/galerij/toggle", methods=["POST"])
@admin_required
def admin_galerij_toggle():
    filename = request.form.get("filename", "")
    team_id = request.form.get("team_id", type=int)
    participant_name = request.form.get("participant_name", "")
    source_label = request.form.get("source_label", "")

    existing = VotingPhoto.query.filter_by(photo_filename=filename).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"action": "removed"})
    else:
        max_order = db.session.query(db.func.max(VotingPhoto.order_index)).scalar() or -1
        vp = VotingPhoto(
            photo_filename=filename,
            team_id=team_id or None,
            participant_name=participant_name,
            source_label=source_label,
            order_index=max_order + 1,
        )
        db.session.add(vp)
        db.session.commit()
        return jsonify({"action": "added", "id": vp.id})


@app.route("/admin/galerij/move/<int:vp_id>", methods=["POST"])
@admin_required
def admin_galerij_move(vp_id):
    direction = request.form.get("direction")
    photos = get_ordered_voting_photos()
    idx = next((i for i, v in enumerate(photos) if v.id == vp_id), None)
    if idx is None:
        return jsonify({"ok": False})
    if direction == "up" and idx > 0:
        photos[idx].order_index, photos[idx - 1].order_index = \
            photos[idx - 1].order_index, photos[idx].order_index
    elif direction == "down" and idx < len(photos) - 1:
        photos[idx].order_index, photos[idx + 1].order_index = \
            photos[idx + 1].order_index, photos[idx].order_index
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin: stemming
# ---------------------------------------------------------------------------

@app.route("/admin/stemming")
@admin_required
def admin_stemming():
    vs = get_voting_session()
    photos = get_ordered_voting_photos()
    teams = {t.id: t for t in Team.query.all()}
    return render_template(
        "admin/stemming.html",
        vs=vs, photos=photos, teams=teams,
        MAX_VOTES=MAX_VOTES,
        cur=VotingPhoto.query.get(vs.current_photo_id) if vs.current_photo_id else None,
    )


@app.route("/admin/stemming/preview", methods=["POST"])
@admin_required
def admin_stemming_start_preview():
    photos = get_ordered_voting_photos()
    if not photos:
        flash("Selecteer eerst foto's in de galerij.", "warning")
        return redirect(url_for("admin_stemming"))
    vs = get_voting_session()
    vs.status = "preview"
    vs.current_photo_id = photos[0].id
    vs.started_at = datetime.utcnow()
    db.session.commit()
    flash("▶️ Voorvertoning gestart.", "success")
    return redirect(url_for("admin_stemming"))


@app.route("/admin/stemming/startvoting", methods=["POST"])
@admin_required
def admin_stemming_start_voting():
    photos = get_ordered_voting_photos()
    if not photos:
        flash("Geen foto's geselecteerd.", "warning")
        return redirect(url_for("admin_stemming"))
    vs = get_voting_session()
    vs.status = "voting"
    vs.current_photo_id = photos[0].id
    db.session.commit()
    flash("🗳️ Stemronde gestart!", "success")
    return redirect(url_for("admin_stemming"))


@app.route("/admin/stemming/navigate", methods=["POST"])
@admin_required
def admin_stemming_navigate():
    direction = request.form.get("direction")  # 'next' or 'prev'
    vs = get_voting_session()
    photos = get_ordered_voting_photos()
    if not photos or vs.status not in ("preview", "voting"):
        return redirect(url_for("admin_stemming"))
    ids = [p.id for p in photos]
    cur_idx = ids.index(vs.current_photo_id) if vs.current_photo_id in ids else 0
    if direction == "next" and cur_idx < len(ids) - 1:
        vs.current_photo_id = ids[cur_idx + 1]
    elif direction == "prev" and cur_idx > 0:
        vs.current_photo_id = ids[cur_idx - 1]
    db.session.commit()
    return redirect(url_for("admin_stemming"))


@app.route("/admin/stemming/finish", methods=["POST"])
@admin_required
def admin_stemming_finish():
    vs = get_voting_session()
    vs.status = "finished"
    vs.finished_at = datetime.utcnow()
    vs.current_photo_id = None
    db.session.commit()
    flash("🏆 Stemming afgesloten! Resultaten zijn zichtbaar.", "success")
    return redirect(url_for("admin_stemming"))


@app.route("/admin/stemming/reset", methods=["POST"])
@admin_required
def admin_stemming_reset():
    vs = get_voting_session()
    vs.status = "setup"
    vs.current_photo_id = None
    vs.started_at = None
    vs.finished_at = None
    Vote.query.delete()
    db.session.commit()
    flash("🔄 Stemming gereset.", "info")
    return redirect(url_for("admin_stemming"))


@app.route("/admin/stemming/beamer")
@admin_required
def admin_stemming_beamer():
    return render_template("admin/stemming_beamer.html")


@app.route("/admin/stemming/status")
@admin_required
def admin_stemming_status():
    vs = get_voting_session()
    photos = get_ordered_voting_photos()
    data = voting_status_dict(vs, photos)
    # Add per-photo vote counts for the control panel
    data["photo_votes"] = {vp.id: vp.vote_count for vp in photos}
    data["total_voters"] = Participant.query.filter_by(checked_in=True).count()
    return jsonify(data)


# ---------------------------------------------------------------------------
# Participant: stemming
# ---------------------------------------------------------------------------

@app.route("/game/stemming")
def participant_stemming():
    token = session.get("participant_token")
    if not token:
        return redirect(url_for("participant_index"))
    p = Participant.query.filter_by(session_token=token).first()
    if not p:
        return redirect(url_for("participant_index"))
    vs = get_voting_session()
    team = p.team
    votes_used = Vote.query.filter_by(participant_id=p.id).count()
    return render_template(
        "participant/stemming.html",
        participant=p, team=team,
        vs=vs, MAX_VOTES=MAX_VOTES,
        votes_used=votes_used,
        votes_left=MAX_VOTES - votes_used,
    )


@app.route("/game/stemming/status")
def participant_stemming_status():
    token = session.get("participant_token")
    if not token:
        return jsonify({"error": "not_logged_in"}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p:
        return jsonify({"error": "no_participant"}), 400

    vs = get_voting_session()
    photos = get_ordered_voting_photos()
    team_id = p.team.id if p.team else None
    data = voting_status_dict(vs, photos, for_team_id=team_id)

    votes_used = Vote.query.filter_by(participant_id=p.id).count()
    data["votes_used"] = votes_used
    data["votes_left"] = MAX_VOTES - votes_used

    if data["current"] and vs.status == "voting":
        cur_id = vs.current_photo_id
        already_voted = Vote.query.filter_by(
            participant_id=p.id, voting_photo_id=cur_id
        ).first() is not None
        is_own_team = (data["current"]["team_id"] == team_id)
        data["current"]["already_voted"] = already_voted
        data["current"]["is_own_team"] = is_own_team
        data["current"]["can_vote"] = (
            data["current"]["can_vote_if_eligible"]
            and not already_voted
            and votes_used < MAX_VOTES
        )
    return jsonify(data)


@app.route("/game/stemming/vote", methods=["POST"])
def participant_stemming_vote():
    token = session.get("participant_token")
    if not token:
        return jsonify({"success": False, "message": "Niet ingelogd."}), 401
    p = Participant.query.filter_by(session_token=token).first()
    if not p:
        return jsonify({"success": False, "message": "Deelnemer niet gevonden."}), 400

    vs = get_voting_session()
    if vs.status != "voting":
        return jsonify({"success": False, "message": "Stemronde is niet actief."}), 400

    voting_photo_id = vs.current_photo_id
    if not voting_photo_id:
        return jsonify({"success": False, "message": "Geen actieve foto."}), 400

    vp = VotingPhoto.query.get(voting_photo_id)
    if not vp:
        return jsonify({"success": False, "message": "Foto niet gevonden."}), 400

    team_id = p.team.id if p.team else None
    if vp.team_id and vp.team_id == team_id:
        return jsonify({"success": False,
                        "message": "Je mag niet stemmen op een foto van je eigen team."}), 400

    votes_used = Vote.query.filter_by(participant_id=p.id).count()
    if votes_used >= MAX_VOTES:
        return jsonify({"success": False, "message": "Je hebt geen stemmen meer over."}), 400

    already = Vote.query.filter_by(
        participant_id=p.id, voting_photo_id=voting_photo_id
    ).first()
    if already:
        return jsonify({"success": False, "message": "Je hebt al op deze foto gestemd."}), 400

    db.session.add(Vote(participant_id=p.id, voting_photo_id=voting_photo_id))
    db.session.commit()

    votes_left = MAX_VOTES - votes_used - 1
    return jsonify({
        "success": True,
        "votes_left": votes_left,
        "vote_count": vp.vote_count,
        "message": f"❤️ Stem uitgebracht! Nog {votes_left} {'stem' if votes_left == 1 else 'stemmen'} over.",
    })


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

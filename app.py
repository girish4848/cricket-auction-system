import copy
import json
import math
import os
import random
import threading
import time

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

from sqlalchemy import inspect, text

from models import (
    AuctionArchive,
    AuctionState,
    LastSaleUndo,
    Player,
    SaleRecord,
    Team,
    db,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-auction-secret-change-me")

_database_url = os.environ.get("DATABASE_URL", "sqlite:///database.db")
if _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

UPLOAD_FOLDER = "static/uploads"
CAPTAINS_UPLOAD_SUBDIR = "captains"
PLAYER_FACE_SUBDIR = "faces"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, CAPTAINS_UPLOAD_SUBDIR), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, PLAYER_FACE_SUBDIR), exist_ok=True)

# Admin access:
# - Dual accounts (recommended on Render): set ADMIN1_PASSWORD and/or ADMIN2_PASSWORD.
#   Login with username admin1 / admin2 and the matching password. Both can be logged in
#   at once (separate sessions). Destructive confirmations use that user's password.
# - Legacy single password: leave ADMIN1_PASSWORD and ADMIN2_PASSWORD unset; then
#   ADMIN_PASSWORD (default "admin") is the only password — no username field.
ADMIN_PASSWORD_LEGACY = os.environ.get("ADMIN_PASSWORD", "admin")


def _load_admin_accounts() -> dict[str, str]:
    """Map login name -> password for enabled staff admins."""
    out: dict[str, str] = {}
    p1 = os.environ.get("ADMIN1_PASSWORD", "").strip()
    p2 = os.environ.get("ADMIN2_PASSWORD", "").strip()
    if p1:
        out["admin1"] = p1
    if p2:
        out["admin2"] = p2
    return out


ADMIN_ACCOUNTS = _load_admin_accounts()

# Remote auction: HOST defaults to 0.0.0.0. Render: set DATABASE_URL (PostgreSQL adds psycopg2-binary).
# Mount persistent disk on static/uploads if captain/player images must survive redeploys.
# Real-time: Procfile uses gunicorn -w 1 so all WebSocket clients share one process.
# If you add more workers/instances, set REDIS_URL and redis so Socket.IO can broadcast
# across processes (message_queue).

db.init_app(app)


@app.context_processor
def branding_assets():
    png_path = os.path.join(app.root_path, "static", "images", "gladiators-logo.png")
    return {"use_gladiators_png": os.path.isfile(png_path)}


@app.context_processor
def admin_ui_context():
    return {
        "multi_admin_login": bool(ADMIN_ACCOUNTS),
        "session_admin_user": session.get("admin_user"),
    }


# Default "threading" is stable for local `py app.py` (Windows-friendly, no Eventlet warning).
# Production (Gunicorn + eventlet worker): set SOCKETIO_ASYNC_MODE=eventlet — Procfile does this on Linux.
_socket_async = os.environ.get("SOCKETIO_ASYNC_MODE", "threading")
_redis_url = os.environ.get("REDIS_URL")
_socket_kw: dict = {"cors_allowed_origins": "*", "async_mode": _socket_async}
if _redis_url:
    _socket_kw["message_queue"] = _redis_url
socketio = SocketIO(app, **_socket_kw)

_bid_lock = threading.Lock()

CATEGORY_PRICES = {1: 500, 2: 400, 3: 300, 4: 200, 5: 100}

TIMER_SECONDS = 40
HEARTBEAT_LAST_SECONDS = 10
MAX_SQUAD = 7
TOTAL_TEAM_POINTS = 10000


def calculate_increment(bid: int) -> int:
    if bid <= 1000:
        return 50
    if bid <= 4000:
        return 100
    return 200


def get_team_limits(team):
    needed = max(0, MAX_SQUAD - team.players_bought)
    reserve = needed * 100
    max_bid = team.remaining_points - reserve
    return reserve, max_bid, needed


app.jinja_env.globals.update(get_team_limits=get_team_limits)


def admin_required():
    return session.get("admin") is True


def admin_password_accepted(username: str, password: str) -> bool:
    if ADMIN_ACCOUNTS:
        u = (username or "").strip().lower()
        return u in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[u] == password
    return password == ADMIN_PASSWORD_LEGACY


def delete_password_ok() -> bool:
    pwd = request.form.get("confirm_password", "")
    if ADMIN_ACCOUNTS:
        who = session.get("admin_user")
        if not who or who not in ADMIN_ACCOUNTS:
            return False
        return ADMIN_ACCOUNTS[who] == pwd
    return pwd == ADMIN_PASSWORD_LEGACY


def remove_upload_rel(rel) -> None:
    if not rel:
        return
    normalized = os.path.normpath(rel.replace("/", os.sep))
    if normalized.startswith(".." + os.sep) or normalized == "..":
        return
    base_abs = os.path.abspath(app.config["UPLOAD_FOLDER"])
    target_abs = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], normalized))
    if not target_abs.startswith(base_abs + os.sep):
        return
    if os.path.isfile(target_abs):
        try:
            os.remove(target_abs)
        except OSError:
            pass


def save_player_face_upload(file_storage) -> str | None:
    """Save optional square/portrait face image under uploads/faces/. Returns relative path or None."""
    if not file_storage or not file_storage.filename:
        return None
    fn = secure_filename(file_storage.filename)
    if not fn:
        return None
    face_dir = os.path.join(app.config["UPLOAD_FOLDER"], PLAYER_FACE_SUBDIR)
    os.makedirs(face_dir, exist_ok=True)
    dest = os.path.join(face_dir, fn)
    file_storage.save(dest)
    return f"{PLAYER_FACE_SUBDIR}/{fn}"


def restore_sale_record(row: SaleRecord) -> None:
    player = Player.query.get(row.player_id)
    team = Team.query.get(row.team_id)
    if player:
        player.status = "unsold"
        player.team_id = None
        player.sold_price = None
        player.eligible_for_random_pool = True
    if team:
        team.remaining_points = row.team_remaining_before
        team.players_bought = row.team_players_bought_before
    row.undone = True


def _undo_legacy_last_sale_row(row: LastSaleUndo) -> None:
    player = Player.query.get(row.player_id)
    team = Team.query.get(row.team_id)
    if not player or not team:
        db.session.delete(row)
        return

    player.status = "unsold"
    player.team_id = None
    player.sold_price = None
    player.eligible_for_random_pool = True

    team.remaining_points = row.team_remaining_before
    team.players_bought = row.team_players_bought_before

    db.session.delete(row)


def undo_sold_player_without_sale_record(pid: int) -> bool:
    player = Player.query.get(pid)
    if not player or player.status != "sold":
        return False
    team = Team.query.get(player.team_id)
    amt = player.sold_price or 0
    if team:
        team.remaining_points += amt
        team.players_bought = max(0, team.players_bought - 1)
    player.status = "unsold"
    player.team_id = None
    player.sold_price = None
    player.eligible_for_random_pool = True
    return True


def apply_sale(player: Player, team: Team, amount: int) -> None:
    rem_before = team.remaining_points
    bought_before = team.players_bought

    player.status = "sold"
    player.team_id = team.id
    player.sold_price = amount

    team.remaining_points -= amount
    team.players_bought += 1

    db.session.add(
        SaleRecord(
            player_id=player.id,
            team_id=team.id,
            amount=amount,
            team_remaining_before=rem_before,
            team_players_bought_before=bought_before,
            undone=False,
        )
    )


def clear_lot(state: AuctionState) -> None:
    state.current_player_id = None
    state.current_bid = 0
    state.current_team_id = None
    state.auction_status = "idle"
    state.timer = 0
    state.timer_deadline = None
    state.last_bid_team_id = None


def live_timer_seconds(state: AuctionState):
    if state.auction_status == "running" and state.timer_deadline is not None:
        return max(0, int(math.ceil(state.timer_deadline - time.time())))
    return None


def lot_display_live(state: AuctionState) -> bool:
    """True while a lot is on the block (waiting or running with a player)."""
    return bool(
        state.full_auction_started
        and state.current_player_id
        and state.auction_status in ("waiting", "running")
    )


def emit_auction_sound(kind: str):
    socketio.emit("auction_sound", {"kind": kind})


def build_auction_snapshot_dict() -> dict:
    teams = Team.query.order_by(Team.id).all()
    snap: dict = {"finished_at": time.time(), "teams": []}
    for t in teams:
        squad = []
        for p in (
            Player.query.filter_by(team_id=t.id, status="sold")
            .order_by(Player.id)
            .all()
        ):
            squad.append(
                {
                    "name": p.name,
                    "price": p.sold_price,
                    "card": p.player_card or "",
                    "photo": p.player_photo or "",
                    "category": p.category,
                }
            )
        snap["teams"].append(
            {
                "id": t.id,
                "name": t.team_name,
                "captain_username": (t.username or "").strip(),
                "captain_photo": t.captain_photo or "",
                "squad": squad,
            }
        )
    return snap


FINALS_TEAM_THEMES = frozenset({"multi", "blue", "red", "yellow"})


def augment_finals_snapshot(snapshot: dict | None) -> dict | None:
    """Sort squad by category; card colour & captain login come from live Team rows."""
    if not snapshot or not snapshot.get("teams"):
        return snapshot
    out = copy.deepcopy(snapshot)
    for tm in out["teams"]:
        squad = tm.get("squad") or []
        squad_sorted = sorted(
            squad,
            key=lambda pl: (
                pl.get("category") if pl.get("category") is not None else 999,
                (pl.get("name") or "").lower(),
            ),
        )
        tm["squad"] = squad_sorted
        tid = tm.get("id")
        db_team = Team.query.get(tid) if tid else None
        if db_team:
            tm["captain_username"] = (db_team.username or "").strip() or tm.get(
                "captain_username", ""
            )
            th = (getattr(db_team, "finals_team_theme", None) or "multi").strip().lower()
            tm["card_theme"] = th if th in FINALS_TEAM_THEMES else "multi"
        else:
            tm.setdefault("captain_username", tm.get("captain_username", ""))
            th = (tm.get("card_theme") or "multi").strip().lower()
            tm["card_theme"] = th if th in FINALS_TEAM_THEMES else "multi"
    return out


def save_auction_archive() -> None:
    payload = build_auction_snapshot_dict()
    db.session.add(
        AuctionArchive(
            finished_at=payload["finished_at"],
            snapshot_json=json.dumps(payload),
        )
    )


def maybe_complete_auction() -> bool:
    state = AuctionState.query.first()
    if not state or getattr(state, "auction_complete", False):
        return False
    if Player.query.count() == 0:
        return False
    if Player.query.filter_by(status="unsold").count() > 0:
        return False
    state.auction_complete = True
    state.full_auction_started = False
    clear_lot(state)
    save_auction_archive()
    return True


def broadcast_state():
    state = AuctionState.query.first()
    player = None
    if state.current_player_id:
        player = Player.query.get(state.current_player_id)

    teams = Team.query.order_by(Team.id).all()
    leading_name = None
    if state.current_team_id:
        lt = Team.query.get(state.current_team_id)
        leading_name = lt.team_name if lt else None

    next_inc = calculate_increment(state.current_bid) if player else 0
    emit_timer = live_timer_seconds(state)

    sold_payload = []
    for p in Player.query.filter_by(status="sold").order_by(Player.id).all():
        t_name = None
        if p.team_id:
            tm = Team.query.get(p.team_id)
            t_name = tm.team_name if tm else None
        sold_payload.append(
            {
                "player_id": p.id,
                "name": p.name,
                "team": t_name,
                "price": p.sold_price,
            }
        )

    unsold_payload = [
        {
            "id": u.id,
            "name": u.name,
            "base_price": u.base_price,
            "category": u.category,
        }
        for u in Player.query.filter_by(status="unsold").order_by(Player.id).all()
    ]

    teams_payload = []
    for t in teams:
        squad = [
            {"id": p.id, "name": p.name, "price": p.sold_price}
            for p in Player.query.filter_by(team_id=t.id, status="sold").order_by(
                Player.id
            ).all()
        ]
        teams_payload.append(
            {
                "id": t.id,
                "name": t.team_name,
                "points": t.remaining_points,
                "players": t.players_bought,
                "ready": t.is_ready,
                "reserve": get_team_limits(t)[0],
                "max_bid": get_team_limits(t)[1],
                "needed": get_team_limits(t)[2],
                "captain_photo": t.captain_photo or "",
                "squad": squad,
            }
        )

    opening_claim = bool(
        player
        and state.full_auction_started
        and state.current_team_id is None
        and state.auction_status in ("waiting", "running")
    )

    socketio.emit(
        "auction_state_update",
        {
            "player": (
                {
                    "id": player.id,
                    "name": player.name,
                    "card": player.player_card,
                    "base_price": player.base_price,
                    "category": player.category,
                }
                if player
                else None
            ),
            "bid": state.current_bid,
            "leading_team": leading_name,
            "leading_team_id": state.current_team_id,
            "last_bid_team_id": getattr(state, "last_bid_team_id", None),
            "opening_claim": opening_claim,
            "claim_amount": player.base_price if opening_claim else None,
            "timer": emit_timer,
            "status": state.auction_status,
            "full_auction_started": state.full_auction_started,
            "next_increment": next_inc,
            "teams": teams_payload,
            "sold": sold_payload,
            "unsold": unsold_payload,
            "captains_ready": all(t.is_ready for t in teams) if teams else True,
            "auction_complete": getattr(state, "auction_complete", False),
            "timer_seconds": TIMER_SECONDS,
            "heartbeat_last_seconds": HEARTBEAT_LAST_SECONDS,
        },
    )


def finalize_sale_from_timer():
    with app.app_context():
        state = AuctionState.query.first()
        if state.auction_status != "running":
            return
        if state.timer_deadline is None:
            return

        if not state.current_player_id or not state.current_team_id:
            state.auction_status = "waiting"
            state.timer_deadline = None
            state.timer = 0
            db.session.commit()
            broadcast_state()
            return

        player = Player.query.get(state.current_player_id)
        team = Team.query.get(state.current_team_id)
        if not player or not team:
            state.auction_status = "waiting"
            state.timer_deadline = None
            state.timer = 0
            db.session.commit()
            broadcast_state()
            return

        apply_sale(player, team, state.current_bid)
        price = state.current_bid
        pname = player.name
        tname = team.team_name

        clear_lot(state)
        finished = maybe_complete_auction()
        db.session.commit()

        socketio.emit(
            "player_sold",
            {"player": pname, "team": tname, "price": price},
        )
        if finished:
            emit_auction_sound("auction_complete")
        broadcast_state()


def auction_timer_loop():
    while True:
        socketio.sleep(1)
        try:
            with app.app_context():
                state = AuctionState.query.first()
                if (
                    not state
                    or state.auction_status != "running"
                    or state.timer_deadline is None
                ):
                    continue

                rem = max(0, int(math.ceil(state.timer_deadline - time.time())))
                state.timer = rem
                db.session.commit()
                broadcast_state()

                if rem <= 0:
                    finalize_sale_from_timer()
        except Exception as exc:
            print("auction_timer_loop:", exc)


with app.app_context():
    db.create_all()

    try:
        inspector = inspect(db.engine)
        col_names = [c["name"] for c in inspector.get_columns("auction_state")]
        if "timer_deadline" not in col_names:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE auction_state ADD COLUMN timer_deadline FLOAT"))
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        team_cols = [c["name"] for c in inspector.get_columns("team")]
        if "captain_photo" not in team_cols:
            with db.engine.connect() as conn:
                conn.execute(
                    text("ALTER TABLE team ADD COLUMN captain_photo VARCHAR(255)")
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        col_names = [c["name"] for c in inspector.get_columns("auction_state")]
        if "last_bid_team_id" not in col_names:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auction_state ADD COLUMN last_bid_team_id INTEGER"
                    )
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        acols = [c["name"] for c in inspector.get_columns("auction_state")]
        if "auction_complete" not in acols:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auction_state ADD COLUMN auction_complete BOOLEAN DEFAULT 0"
                    )
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        pcols = [c["name"] for c in inspector.get_columns("player")]
        if "eligible_for_random_pool" not in pcols:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE player ADD COLUMN eligible_for_random_pool BOOLEAN DEFAULT 1"
                    )
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        acols2 = [c["name"] for c in inspector.get_columns("auction_state")]
        if "finals_card_theme" not in acols2:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auction_state ADD COLUMN finals_card_theme VARCHAR(16) DEFAULT 'auto'"
                    )
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        pcols2 = [c["name"] for c in inspector.get_columns("player")]
        if "player_photo" not in pcols2:
            with db.engine.connect() as conn:
                conn.execute(
                    text("ALTER TABLE player ADD COLUMN player_photo VARCHAR(255)")
                )
                conn.commit()
    except Exception:
        pass

    try:
        inspector = inspect(db.engine)
        team_cols_th = [c["name"] for c in inspector.get_columns("team")]
        if "finals_team_theme" not in team_cols_th:
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE team ADD COLUMN finals_team_theme VARCHAR(16) DEFAULT 'multi'"
                    )
                )
                conn.commit()
    except Exception:
        pass

    if not AuctionState.query.first():
        db.session.add(
            AuctionState(
                current_player_id=None,
                current_bid=0,
                current_team_id=None,
                auction_status="idle",
                timer=0,
                timer_deadline=None,
                full_auction_started=False,
                last_bid_team_id=None,
                auction_complete=False,
                finals_card_theme="auto",
            )
        )
        db.session.commit()

socketio.start_background_task(auction_timer_loop)


@app.before_request
def ensure_sessions():
    session.permanent = True


def reset_all_ready_flags():
    for t in Team.query.all():
        t.is_ready = False


@app.route("/")
def home():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login():
    team = Team.query.filter_by(
        username=request.form.get("username", ""),
        password=request.form.get("password", ""),
    ).first()

    if team:
        session["team_id"] = team.id
        return redirect(url_for("auction"))

    flash("Invalid username or password", "error")
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.pop("team_id", None)
    return redirect(url_for("home"))


@app.route("/auction")
def auction():
    if "team_id" not in session:
        return redirect(url_for("home"))

    team = Team.query.get(session["team_id"])
    if not team:
        session.pop("team_id", None)
        return redirect(url_for("home"))

    state = AuctionState.query.first()
    reserve, max_bid, needed = get_team_limits(team)
    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)
    return render_template(
        "auction.html",
        team=team,
        state=state,
        current_player=current_player,
        reserve=reserve,
        max_bid=max_bid,
        needed=needed,
        next_increment=calculate_increment(state.current_bid),
        timer_seconds=TIMER_SECONDS,
        timer_live=live_timer_seconds(state),
        lot_live=lot_display_live(state),
        finals_team_theme=getattr(team, "finals_team_theme", None) or "multi",
    )


@app.route("/captain/finals_theme", methods=["POST"])
def captain_set_finals_theme():
    if "team_id" not in session:
        return redirect(url_for("home"))
    raw = (request.form.get("theme") or "multi").strip().lower()
    if raw not in FINALS_TEAM_THEMES:
        raw = "multi"
    team = Team.query.get(session["team_id"])
    if team:
        team.finals_team_theme = raw
        db.session.commit()
        flash("Final squad card colour saved for your team.", "ok")
    return redirect(url_for("auction"))


@app.route("/captain/roster")
def captain_roster():
    if "team_id" not in session:
        return redirect(url_for("home"))

    team = Team.query.get(session["team_id"])
    if not team:
        session.pop("team_id", None)
        return redirect(url_for("home"))

    teams = Team.query.order_by(Team.id).all()
    team_names = {t.id: t.team_name for t in teams}
    players = Player.query.order_by(Player.id).all()
    roster_rows = []
    for p in players:
        roster_rows.append(
            {
                "id": p.id,
                "name": p.name,
                "category": p.category,
                "base_price": p.base_price,
                "status": p.status,
                "sold_price": p.sold_price,
                "buyer_team": team_names.get(p.team_id) if p.team_id else None,
                "card": p.player_card or "",
            }
        )

    return render_template(
        "captain_roster.html",
        team=team,
        roster_rows=roster_rows,
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    enabled = sorted(ADMIN_ACCOUNTS.keys())
    if request.method == "POST":
        pwd = request.form.get("password") or ""
        user = (request.form.get("username") or "").strip().lower()
        if admin_password_accepted(user, pwd):
            session["admin"] = True
            session["admin_user"] = user if ADMIN_ACCOUNTS else None
            return redirect(url_for("admin_dashboard"))
        flash("Wrong username or password", "error")
    return render_template(
        "admin_login.html",
        admin_accounts_enabled=enabled,
    )


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_user", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    return render_template(
        "admin_dashboard.html",
        teams=Team.query.order_by(Team.id).all(),
        players=Player.query.order_by(Player.id).all(),
        sold_count=Player.query.filter_by(status="sold").count(),
    )


@app.route("/admin/teams", methods=["GET", "POST"])
def admin_teams():
    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        photo_rel = None
        cap_file = request.files.get("captain_photo")
        if cap_file and cap_file.filename:
            fn = secure_filename(cap_file.filename)
            if fn:
                capt_dir = os.path.join(app.config["UPLOAD_FOLDER"], CAPTAINS_UPLOAD_SUBDIR)
                os.makedirs(capt_dir, exist_ok=True)
                dest = os.path.join(capt_dir, fn)
                cap_file.save(dest)
                photo_rel = f"{CAPTAINS_UPLOAD_SUBDIR}/{fn}"

        db.session.add(
            Team(
                team_name=request.form["team_name"],
                username=request.form["username"],
                password=request.form["password"],
                remaining_points=TOTAL_TEAM_POINTS,
                total_points=TOTAL_TEAM_POINTS,
                captain_photo=photo_rel,
            )
        )
        db.session.commit()
        flash("Team added", "ok")
        return redirect(url_for("admin_teams"))

    return render_template(
        "admin_teams.html", teams=Team.query.order_by(Team.id).all()
    )


@app.route("/admin/players", methods=["GET", "POST"])
def admin_players():
    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        category = int(request.form["category"])
        card = request.files.get("card")
        photo_rel = save_player_face_upload(request.files.get("player_photo"))

        if not card or not card.filename:
            flash("Player card image is required", "error")
            return redirect(url_for("admin_players"))

        filename = secure_filename(card.filename)
        card.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        db.session.add(
            Player(
                name=request.form["name"],
                category=category,
                base_price=CATEGORY_PRICES[category],
                player_card=filename,
                player_photo=photo_rel,
                status="unsold",
            )
        )
        db.session.commit()
        flash("Player added", "ok")
        return redirect(url_for("admin_players"))

    return render_template(
        "admin_players.html",
        players=Player.query.order_by(Player.id).all(),
        category_prices=CATEGORY_PRICES,
    )


@app.route("/admin/teams/<int:tid>/edit", methods=["GET", "POST"])
def admin_edit_team(tid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    team = Team.query.get_or_404(tid)

    if request.method == "POST":
        team_name = request.form.get("team_name", "").strip()
        username = request.form.get("username", "").strip()
        new_password = request.form.get("password", "").strip()

        if not team_name or not username:
            flash("Team name and captain username are required", "error")
            return redirect(url_for("admin_edit_team", tid=tid))

        taken = Team.query.filter(Team.username == username, Team.id != tid).first()
        if taken:
            flash("That captain username is already used by another team", "error")
            return redirect(url_for("admin_edit_team", tid=tid))

        team.team_name = team_name
        team.username = username
        if new_password:
            team.password = new_password

        cap_file = request.files.get("captain_photo")
        if cap_file and cap_file.filename:
            fn = secure_filename(cap_file.filename)
            if fn:
                capt_dir = os.path.join(app.config["UPLOAD_FOLDER"], CAPTAINS_UPLOAD_SUBDIR)
                os.makedirs(capt_dir, exist_ok=True)
                old_photo = team.captain_photo
                dest = os.path.join(capt_dir, fn)
                cap_file.save(dest)
                team.captain_photo = f"{CAPTAINS_UPLOAD_SUBDIR}/{fn}"
                if old_photo and old_photo != team.captain_photo:
                    remove_upload_rel(old_photo)

        db.session.commit()
        flash("Team updated", "ok")
        return redirect(url_for("admin_teams"))

    return render_template("admin_team_edit.html", team=team)


@app.route("/admin/teams/<int:tid>/delete", methods=["GET", "POST"])
def admin_delete_team(tid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    team = Team.query.get_or_404(tid)

    if request.method == "POST":
        if not delete_password_ok():
            flash("Wrong password — team was not deleted.", "error")
            return redirect(url_for("admin_delete_team", tid=tid))

        state = AuctionState.query.first()
        if state and state.current_team_id == tid:
            flash("Cannot delete this team while they hold the current bid.", "error")
            return redirect(url_for("admin_teams"))

        if Player.query.filter_by(team_id=tid).first():
            flash("Cannot delete a team that has players assigned (sold squad).", "error")
            return redirect(url_for("admin_teams"))

        LastSaleUndo.query.filter_by(team_id=tid).delete()
        photo = team.captain_photo
        db.session.delete(team)
        db.session.commit()
        remove_upload_rel(photo)
        flash("Team deleted", "ok")
        return redirect(url_for("admin_teams"))

    return render_template("admin_team_delete_confirm.html", team=team)


def _player_is_current_lot(pid: int) -> bool:
    st = AuctionState.query.first()
    return bool(st and st.current_player_id == pid)


@app.route("/admin/players/<int:pid>/edit", methods=["GET", "POST"])
def admin_edit_player(pid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    player = Player.query.get_or_404(pid)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Display name is required", "error")
            return redirect(url_for("admin_edit_player", pid=pid))

        player.name = name
        on_block = _player_is_current_lot(pid)

        if player.status == "sold":
            card = request.files.get("card")
            if card and card.filename:
                fn = secure_filename(card.filename)
                if fn:
                    old = player.player_card
                    card.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
                    player.player_card = fn
                    if old and old != player.player_card:
                        remove_upload_rel(old)
            pr = save_player_face_upload(request.files.get("player_photo"))
            if pr:
                oldp = player.player_photo
                player.player_photo = pr
                if oldp and oldp != player.player_photo:
                    remove_upload_rel(oldp)
            db.session.commit()
            flash("Player updated (sold — category locked)", "ok")
            return redirect(url_for("admin_players"))

        category = int(request.form["category"])
        if category not in CATEGORY_PRICES:
            flash("Invalid category", "error")
            return redirect(url_for("admin_edit_player", pid=pid))

        if on_block and category != player.category:
            flash("Cannot change category while this player is the active auction lot.", "error")
            return redirect(url_for("admin_edit_player", pid=pid))

        player.category = category
        player.base_price = CATEGORY_PRICES[category]

        card = request.files.get("card")
        if card and card.filename:
            fn = secure_filename(card.filename)
            if fn:
                old = player.player_card
                card.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
                player.player_card = fn
                if old and old != player.player_card:
                    remove_upload_rel(old)

        pr = save_player_face_upload(request.files.get("player_photo"))
        if pr:
            oldp = player.player_photo
            player.player_photo = pr
            if oldp and oldp != player.player_photo:
                remove_upload_rel(oldp)

        if on_block:
            st = AuctionState.query.first()
            if st and st.auction_status == "waiting":
                st.current_bid = player.base_price

        db.session.commit()
        flash("Player updated", "ok")
        return redirect(url_for("admin_players"))

    return render_template(
        "admin_player_edit.html",
        player=player,
        category_prices=CATEGORY_PRICES,
        on_auction_block=_player_is_current_lot(pid),
    )


@app.route("/admin/players/<int:pid>/delete", methods=["GET", "POST"])
def admin_delete_player(pid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    player = Player.query.get_or_404(pid)

    if request.method == "POST":
        if not delete_password_ok():
            flash("Wrong password — player was not deleted.", "error")
            return redirect(url_for("admin_delete_player", pid=pid))

        if player.status == "sold":
            flash("Cannot delete a sold player.", "error")
            return redirect(url_for("admin_players"))

        state = AuctionState.query.first()
        if state and state.current_player_id == pid:
            flash("Cannot delete the active auction lot — stop or finish the lot first.", "error")
            return redirect(url_for("admin_players"))

        card = player.player_card
        face = player.player_photo
        db.session.delete(player)
        db.session.commit()
        remove_upload_rel(face)
        remove_upload_rel(card)
        flash("Player deleted", "ok")
        return redirect(url_for("admin_players"))

    return render_template("admin_player_delete_confirm.html", player=player)


@app.route("/admin/auction")
def admin_auction():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)

    available = Player.query.filter_by(status="unsold").order_by(Player.id).all()
    sold = Player.query.filter_by(status="sold").order_by(Player.id).all()
    teams = Team.query.order_by(Team.id).all()
    ready_all = bool(teams) and all(t.is_ready for t in teams)

    return render_template(
        "admin_auction.html",
        players=available,
        sold_players=sold,
        teams=teams,
        state=state,
        current_player=current_player,
        ready_all=ready_all,
        timer_seconds=TIMER_SECONDS,
        timer_live=live_timer_seconds(state),
        lot_live=lot_display_live(state),
        next_increment=(
            calculate_increment(state.current_bid) if current_player else 0
        ),
    )


@app.route("/start_full_auction")
def start_full_auction():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    if getattr(state, "auction_complete", False):
        flash(
            "This auction run has finished. Restart full auction to begin again.",
            "error",
        )
        return redirect(url_for("admin_auction"))
    state.full_auction_started = True
    db.session.commit()
    broadcast_state()
    flash("Full auction started — captains can mark ready", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/start_player/<int:pid>")
def start_player(pid):
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    player = Player.query.get(pid)

    if getattr(state, "auction_complete", False):
        flash("Auction finished — open Finals or restart.", "error")
        return redirect(url_for("admin_auction"))

    if not player or player.status != "unsold":
        flash("Player not available", "error")
        return redirect(url_for("admin_auction"))

    if not state.full_auction_started:
        flash("Start the full auction first", "error")
        return redirect(url_for("admin_auction"))

    teams = Team.query.all()
    if teams and not all(t.is_ready for t in teams):
        flash("All captains must mark Ready before starting a player", "error")
        return redirect(url_for("admin_auction"))

    state.current_player_id = pid
    state.current_bid = player.base_price
    state.current_team_id = None
    state.auction_status = "waiting"
    state.timer = 0
    state.timer_deadline = None
    state.last_bid_team_id = None

    reset_all_ready_flags()
    db.session.commit()
    broadcast_state()
    emit_auction_sound("lot_start")
    flash(f"Auction started for {player.name}", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/pick_random_player")
def pick_random_player():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    if state and getattr(state, "auction_complete", False):
        flash("Auction already finished — view finals or restart.", "error")
        return redirect(url_for("admin_auction"))

    pool = Player.query.filter_by(
        status="unsold", eligible_for_random_pool=True
    ).all()
    if not pool:
        stale = Player.query.filter_by(
            status="unsold", eligible_for_random_pool=False
        ).count()
        if stale:
            for p in Player.query.filter_by(status="unsold"):
                p.eligible_for_random_pool = True
            db.session.commit()
            pool = Player.query.filter_by(
                status="unsold", eligible_for_random_pool=True
            ).all()
            flash(
                "Random pool now includes players who were marked unsold earlier "
                "(all first-pass unsold picks were exhausted).",
                "ok",
            )
    if not pool:
        flash("No unsold players left", "error")
        return redirect(url_for("admin_auction"))

    choice = random.choice(pool)
    return redirect(url_for("start_player", pid=choice.id))


@app.route("/reset_lot")
def reset_lot():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    player = Player.query.get(state.current_player_id)
    if not player:
        flash("No active player", "error")
        return redirect(url_for("admin_auction"))

    state.current_bid = player.base_price
    state.current_team_id = None
    state.auction_status = "waiting"
    state.timer = 0
    state.timer_deadline = None
    state.last_bid_team_id = None
    db.session.commit()
    broadcast_state()
    flash("Current lot reset to base price — timer paused until next bid", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/manual_sell", methods=["POST"])
def manual_sell():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    player = Player.query.get(int(request.form["player_id"]))
    team = Team.query.get(int(request.form["team_id"]))
    amount = int(request.form["amount"])

    if not player or player.status != "unsold":
        flash("Invalid player", "error")
        return redirect(url_for("admin_auction"))

    if not team:
        flash("Invalid team", "error")
        return redirect(url_for("admin_auction"))

    if state.current_player_id and player.id != state.current_player_id:
        flash("Manual sell only applies to the player currently on the block", "error")
        return redirect(url_for("admin_auction"))

    reserve, max_bid, _ = get_team_limits(team)
    if amount > max_bid:
        flash(f"Amount exceeds max bid ({max_bid}) for that team", "error")
        return redirect(url_for("admin_auction"))

    if amount < player.base_price:
        flash("Amount must be at least base price", "error")
        return redirect(url_for("admin_auction"))

    if state.current_team_id and amount < state.current_bid:
        flash("Amount must be at least the current high bid", "error")
        return redirect(url_for("admin_auction"))

    apply_sale(player, team, amount)
    clear_lot(state)
    finished = maybe_complete_auction()
    db.session.commit()

    socketio.emit(
        "player_sold",
        {"player": player.name, "team": team.team_name, "price": amount},
    )
    if finished:
        emit_auction_sound("auction_complete")
    broadcast_state()
    flash("Sold (manual)", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/sell_current", methods=["POST"])
def sell_current():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    player = Player.query.get(state.current_player_id)
    team = Team.query.get(state.current_team_id)

    if not player or not team:
        flash("Need a leading team and bid to sell at current price", "error")
        return redirect(url_for("admin_auction"))

    reserve, max_bid, _ = get_team_limits(team)
    if state.current_bid > max_bid:
        flash("Current bid exceeds leading team's max bid — choose another action", "error")
        return redirect(url_for("admin_auction"))

    price = state.current_bid
    apply_sale(player, team, price)
    clear_lot(state)
    finished = maybe_complete_auction()
    db.session.commit()

    socketio.emit(
        "player_sold",
        {"player": player.name, "team": team.team_name, "price": price},
    )
    if finished:
        emit_auction_sound("auction_complete")
    broadcast_state()
    flash("Sold at current bid", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/unsold", methods=["POST"])
def unsold():
    if not admin_required():
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    player = Player.query.get(state.current_player_id)

    if player:
        player.status = "unsold"
        player.team_id = None
        player.sold_price = None
        player.eligible_for_random_pool = False

    clear_lot(state)
    db.session.commit()

    pname = player.name if player else ""
    socketio.emit("player_unsold", {"player": pname})

    broadcast_state()
    flash("Player marked unsold for this round", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/undo_last_sale", methods=["POST"])
def undo_last_sale():
    if not admin_required():
        return redirect(url_for("admin_login"))

    row_sr = (
        SaleRecord.query.filter_by(undone=False)
        .order_by(SaleRecord.id.desc())
        .first()
    )
    if row_sr:
        restore_sale_record(row_sr)
        db.session.commit()
        broadcast_state()
        flash("Last sale undone — player returned to pool", "ok")
        return redirect(url_for("admin_auction"))

    row_legacy = LastSaleUndo.query.order_by(LastSaleUndo.id.desc()).first()
    if row_legacy:
        _undo_legacy_last_sale_row(row_legacy)
        db.session.commit()
        broadcast_state()
        flash("Last sale undone — player returned to pool", "ok")
        return redirect(url_for("admin_auction"))

    flash("Nothing to undo", "error")
    return redirect(url_for("admin_auction"))


@app.route("/admin/sale/undo/<int:pid>", methods=["POST"])
def admin_undo_sale(pid):
    if not admin_required():
        return redirect(url_for("admin_login"))

    row_sr = (
        SaleRecord.query.filter_by(player_id=pid, undone=False)
        .order_by(SaleRecord.id.desc())
        .first()
    )
    if row_sr:
        restore_sale_record(row_sr)
        db.session.commit()
        broadcast_state()
        flash("Sale undone — player returned to pool", "ok")
        return redirect(url_for("admin_auction"))

    if undo_sold_player_without_sale_record(pid):
        db.session.commit()
        broadcast_state()
        flash("Sale undone (legacy record)", "ok")
        return redirect(url_for("admin_auction"))

    flash("Nothing to undo for that player", "error")
    return redirect(url_for("admin_auction"))


@app.route("/admin/sale/edit/<int:pid>", methods=["POST"])
def admin_edit_sale(pid):
    if not admin_required():
        return redirect(url_for("admin_login"))

    player = Player.query.get(pid)
    if not player or player.status != "sold":
        flash("That player is not sold — nothing to edit", "error")
        return redirect(url_for("admin_auction"))

    try:
        new_team_id = int(request.form.get("team_id", ""))
        amount = int(request.form.get("amount", ""))
    except ValueError:
        flash("Invalid team or amount", "error")
        return redirect(url_for("admin_auction"))

    new_team = Team.query.get(new_team_id)
    if not new_team:
        flash("Invalid team", "error")
        return redirect(url_for("admin_auction"))

    row_sr = (
        SaleRecord.query.filter_by(player_id=pid, undone=False)
        .order_by(SaleRecord.id.desc())
        .first()
    )
    if row_sr:
        restore_sale_record(row_sr)
    else:
        if not undo_sold_player_without_sale_record(pid):
            flash("Could not revert sale for editing", "error")
            return redirect(url_for("admin_auction"))

    db.session.flush()

    player = Player.query.get(pid)
    if not player or player.status != "unsold":
        db.session.rollback()
        flash("Player state error during edit", "error")
        return redirect(url_for("admin_auction"))

    if amount < player.base_price:
        flash(f"Amount must be at least base price ({player.base_price})", "error")
        db.session.rollback()
        return redirect(url_for("admin_auction"))

    reserve, max_bid, _ = get_team_limits(new_team)
    if amount > max_bid:
        flash(f"Amount exceeds new team's max bid ({max_bid})", "error")
        db.session.rollback()
        return redirect(url_for("admin_auction"))

    apply_sale(player, new_team, amount)
    finished = maybe_complete_auction()
    db.session.commit()
    if finished:
        emit_auction_sound("auction_complete")
    broadcast_state()
    flash("Sale updated", "ok")
    return redirect(url_for("admin_auction"))


@app.route("/finals")
def auction_finals():
    if not admin_required():
        flash("Final squads are only available to admins.", "error")
        return redirect(url_for("admin_login"))

    state = AuctionState.query.first()
    arc = AuctionArchive.query.order_by(AuctionArchive.id.desc()).first()
    if arc:
        data = augment_finals_snapshot(json.loads(arc.snapshot_json))
        return render_template(
            "auction_finals.html",
            snapshot=data,
            stale_results=not getattr(state, "auction_complete", False),
        )
    if state and getattr(state, "auction_complete", False):
        return render_template(
            "auction_finals.html",
            snapshot=augment_finals_snapshot(build_auction_snapshot_dict()),
            stale_results=False,
        )
    return render_template(
        "auction_finals.html",
        snapshot=None,
        stale_results=False,
    )


@app.route("/admin/restart_full_auction", methods=["POST"])
def admin_restart_full_auction():
    if not admin_required():
        return redirect(url_for("admin_login"))

    for p in Player.query.all():
        p.status = "unsold"
        p.team_id = None
        p.sold_price = None
        p.eligible_for_random_pool = True

    for t in Team.query.all():
        t.remaining_points = TOTAL_TEAM_POINTS
        t.total_points = TOTAL_TEAM_POINTS
        t.players_bought = 0
        t.is_ready = False
        t.finals_team_theme = "multi"

    SaleRecord.query.delete()
    LastSaleUndo.query.delete()

    state = AuctionState.query.first()
    clear_lot(state)
    state.full_auction_started = False
    state.auction_complete = False

    db.session.commit()
    broadcast_state()
    flash(
        "Full auction restarted — squads cleared, all players unsold, scores reset.",
        "ok",
    )
    return redirect(url_for("admin_auction"))


@app.route("/admin/team/<int:tid>/ready", methods=["POST"])
def admin_team_ready(tid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    team = Team.query.get_or_404(tid)
    team.is_ready = request.form.get("ready") == "1"
    db.session.commit()
    broadcast_state()
    flash(
        f"{team.team_name}: marked {'ready' if team.is_ready else 'not ready'}.",
        "ok",
    )
    return redirect(url_for("admin_auction"))


@app.route("/live")
def live_board():
    state = AuctionState.query.first()
    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)

    leading = Team.query.get(state.current_team_id) if state.current_team_id else None

    return render_template(
        "live_board.html",
        state=state,
        current_player=current_player,
        leading_team=leading,
        teams=Team.query.order_by(Team.id).all(),
        sold_players=Player.query.filter_by(status="sold").order_by(Player.id).all(),
        timer_seconds=TIMER_SECONDS,
        timer_live=live_timer_seconds(state),
        lot_live=lot_display_live(state),
    )


@app.route("/leaderboard")
def leaderboard():
    teams = Team.query.order_by(Team.remaining_points.desc()).all()
    return render_template("leaderboard.html", teams=teams)


@socketio.on("connect")
def ws_connect():
    broadcast_state()


@socketio.on("request_state")
def send_state():
    broadcast_state()


@socketio.on("captain_ready")
def captain_ready(data):
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    if not team:
        return

    team.is_ready = bool(data.get("ready", True))
    db.session.commit()
    broadcast_state()


def _process_increment_bid(team: Team) -> tuple[bool, str | None]:
    with _bid_lock:
        db.session.expire_all()
        state = AuctionState.query.first()
        if not state.full_auction_started:
            return False, "Auction has not gone live yet."
        if not state.current_player_id:
            return False, "No player is on the block."
        if state.auction_status not in ("waiting", "running"):
            return False, "Bidding is not open for this lot."

        player = Player.query.get(state.current_player_id)
        if not player:
            return False, "Invalid lot."

        lb = getattr(state, "last_bid_team_id", None)
        if lb is not None and lb == team.id:
            return (
                False,
                "Wait for another captain to bid before you can bid again.",
            )

        if getattr(state, "auction_complete", False):
            return False, "Auction has finished."

        if team.players_bought >= MAX_SQUAD:
            return (
                False,
                "Your squad is full (7 players) — you cannot bid anymore.",
            )

        _, max_bid, _ = get_team_limits(team)

        opening_claim = state.current_team_id is None
        if opening_claim:
            new_bid = player.base_price
        else:
            new_bid = state.current_bid + calculate_increment(state.current_bid)

        if new_bid > max_bid:
            return False, "That bid is above your team's maximum for this round."

        if opening_claim:
            if new_bid != state.current_bid or new_bid != player.base_price:
                return (
                    False,
                    f"Opening bid must match the base price ({player.base_price} pts).",
                )
        elif new_bid <= state.current_bid:
            return (
                False,
                "That bid is already live — another captain leads at this price.",
            )

        state.current_bid = new_bid
        state.current_team_id = team.id
        state.last_bid_team_id = team.id
        state.auction_status = "running"
        state.timer_deadline = time.time() + TIMER_SECONDS
        state.timer = TIMER_SECONDS

        db.session.commit()
        broadcast_state()
        emit_auction_sound("bid")
        return True, None


def _process_custom_bid(team: Team, amount: int) -> tuple[bool, str | None]:
    with _bid_lock:
        db.session.expire_all()
        state = AuctionState.query.first()
        if not state.full_auction_started:
            return False, "Auction has not gone live yet."
        if not state.current_player_id:
            return False, "No player is on the block."
        if state.auction_status not in ("waiting", "running"):
            return False, "Bidding is not open for this lot."

        player = Player.query.get(state.current_player_id)
        if not player:
            return False, "Invalid lot."

        lb = getattr(state, "last_bid_team_id", None)
        if lb is not None and lb == team.id:
            return (
                False,
                "Wait for another captain to bid before you can bid again.",
            )

        if getattr(state, "auction_complete", False):
            return False, "Auction has finished."

        if team.players_bought >= MAX_SQUAD:
            return (
                False,
                "Your squad is full (7 players) — you cannot bid anymore.",
            )

        _, max_bid, _ = get_team_limits(team)
        opening_claim = state.current_team_id is None

        if opening_claim:
            if amount != player.base_price or amount != state.current_bid:
                return (
                    False,
                    f"Opening bid must equal the base price ({player.base_price} pts).",
                )
        elif amount <= state.current_bid:
            return (
                False,
                "That bid is already live — another captain leads here. Raise your bid.",
            )

        if amount > max_bid:
            return False, "That bid is above your team's maximum for this round."

        state.current_bid = amount
        state.current_team_id = team.id
        state.last_bid_team_id = team.id
        state.auction_status = "running"
        state.timer_deadline = time.time() + TIMER_SECONDS
        state.timer = TIMER_SECONDS

        db.session.commit()
        broadcast_state()
        emit_auction_sound("bid")
        return True, None


@socketio.on("bid")
def bid():
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    if not team:
        emit("bid_rejected", {"reason": "Team not found."})
        return

    ok, msg = _process_increment_bid(team)
    if not ok:
        emit("bid_rejected", {"reason": msg or "Bid not allowed"})


@socketio.on("custom_bid")
def custom_bid(data):
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    if not team:
        emit("bid_rejected", {"reason": "Team not found."})
        return

    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        emit("bid_rejected", {"reason": "Invalid amount"})
        return

    ok, msg = _process_custom_bid(team, amount)
    if not ok:
        emit("bid_rejected", {"reason": msg or "Bid not allowed"})


if __name__ == "__main__":
    _host = os.environ.get("HOST", "0.0.0.0")
    _port = int(os.environ.get("PORT", "5000"))
    _debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    # Flask's auto-reloader spawns a second process and breaks Socket.IO / background
    # tasks — disable it so the server actually listens instead of hanging.
    socketio.run(
        app,
        debug=_debug,
        host=_host,
        port=_port,
        allow_unsafe_werkzeug=True,
        use_reloader=False,
    )

from flask import Flask, render_template, request, redirect, session
from flask_socketio import SocketIO, emit
from models import db, Team, Player, AuctionState
import os, time
from werkzeug.utils import secure_filename
import os

app = Flask(__name__)
app.secret_key = "auction_secret_key"

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'

UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# 🔥 CREATE FOLDER IF NOT EXISTS (IMPORTANT)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db.init_app(app)
socketio = SocketIO(app,cors_allowed_origins="*", async_mode="eventlet")

# ---------------- CATEGORY ----------------
CATEGORY_PRICES = {
    1: 500,
    2: 400,
    3: 300,
    4: 200,
    5: 100
}

# ---------------- INIT ----------------
with app.app_context():
    db.create_all()

    if not AuctionState.query.first():
        db.session.add(AuctionState(
            current_player_id=None,
            current_bid=0,
            current_team_id=None,
            auction_status="waiting",
            timer=60,
            full_auction_started=False
        ))
        db.session.commit()

# ---------------- HELPERS ----------------
def calculate_increment(bid):
    if bid <= 1000:
        return 50
    elif bid <= 4000:
        return 100
    return 200

def get_team_limits(team):
    needed = 7 - team.players_bought
    reserve = needed * 100
    max_bid = team.remaining_points - reserve
    return reserve, max_bid, needed

app.jinja_env.globals.update(get_team_limits=get_team_limits)

# ---------------- TIMER ENGINE ----------------
def run_timer():
    while True:
        state = AuctionState.query.first()

        if state.auction_status != "running":
            break

        if state.timer <= 0:

            # AUTO SELL
            if state.current_team_id:
                team = Team.query.get(state.current_team_id)
                player = Player.query.get(state.current_player_id)

                player.status = "sold"
                player.team_id = team.id
                player.sold_price = state.current_bid

                team.remaining_points -= state.current_bid
                team.players_bought += 1

                socketio.emit("player_sold", {
                    "player": player.name,
                    "team": team.team_name,
                    "price": player.sold_price
                })

            else:
                socketio.emit("no_bid")

            # RESET STATE
            state.current_player_id = None
            state.current_bid = 0
            state.current_team_id = None
            state.auction_status = "waiting"
            state.timer = 60

            db.session.commit()
            break

        socketio.emit("timer", {"time": state.timer})

        state.timer -= 1
        db.session.commit()

        time.sleep(1)

# ---------------- LOGIN ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        team = Team.query.filter_by(
            username=request.form["username"],
            password=request.form["password"]
        ).first()

        if team:
            session["team_id"] = team.id
            return redirect("/auction")

    return render_template("login.html")

# ---------------- ADMIN DASHBOARD ----------------
@app.route("/admin")
def admin_dashboard():
    return render_template(
        "admin_dashboard.html",
        players=Player.query.all(),
        teams=Team.query.all()
    )

# ---------------- ADMIN AUCTION ----------------
@app.route("/admin/auction")
def admin_auction():
    players = Player.query.filter_by(status="unsold").all()
    teams = Team.query.all()
    state = AuctionState.query.first()

    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)

    return render_template(
        "admin_auction.html",
        players=players,
        teams=teams,
        state=state,
        current_player=current_player
    )

# ---------------- LIVE BOARD ----------------
@app.route("/live")
def live_board():
    return render_template(
        "live_board.html",
        teams=Team.query.all(),
        sold_players=Player.query.filter_by(status="sold").all(),
        state=AuctionState.query.first()
    )

# ---------------- START FULL AUCTION ----------------
@app.route("/start_full_auction")
def start_full_auction():
    state = AuctionState.query.first()
    state.full_auction_started = True
    db.session.commit()

    socketio.emit("auction_started")
    return redirect("/admin/auction")

# ---------------- ADD TEAM ----------------
@app.route("/add_team", methods=["POST"])
def add_team():
    db.session.add(Team(
        team_name=request.form["team_name"],
        username=request.form["username"],
        password=request.form["password"]
    ))
    db.session.commit()
    return redirect("/admin")

# ---------------- ADD PLAYER ----------------
@app.route("/add_player", methods=["POST"])
def add_player():
    category = int(request.form["category"])

    card = request.files["card"]
    filename = card.filename
    card.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    db.session.add(Player(
        name=request.form["name"],
        player_card = filename
    ))
    db.session.add(player)
    db.session.commit()
    return redirect("/admin")

# ---------------- AUCTION PAGE ----------------
@app.route("/auction")
def auction():
    if "team_id" not in session:
        return redirect("/")
    team = Team.query.get(session["team_id"])
    return render_template("auction.html", team=team)

# ---------------- READY SYSTEM ----------------
@socketio.on("ready")
def ready():
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    team.is_ready = True
    db.session.commit()

    socketio.emit("ready_update", {
        "teams": [
            {"name": t.team_name, "ready": t.is_ready}
            for t in Team.query.all()
        ]
    })

# ---------------- START PLAYER ----------------
@app.route("/start_player/<int:pid>")
def start_player(pid):
    state = AuctionState.query.first()
    player = Player.query.get(pid)

    state.current_player_id = pid
    state.current_bid = player.base_price
    state.current_team_id = None
    state.auction_status = "waiting"
    state.timer = 60

    db.session.commit()

    socketio.emit("new_player", {
        "name": player.name,
        "card": player.player_card,
        "base_price": player.base_price
    })

    return redirect("/admin/auction")

# ---------------- BID ----------------
@socketio.on("bid")
def bid():
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    state = AuctionState.query.first()

    inc = calculate_increment(state.current_bid)
    new_bid = state.current_bid + inc

    reserve, max_bid, _ = get_team_limits(team)

    if new_bid > max_bid:
        return

    state.current_bid = new_bid
    state.current_team_id = team.id
    state.auction_status = "running"
    state.timer = 60

    db.session.commit()

    socketio.emit("update_bid", {
        "bid": new_bid,
        "team": team.team_name
    })

    socketio.start_background_task(run_timer)

# ---------------- CUSTOM BID ----------------
@socketio.on("custom_bid")
def custom_bid(data):
    if "team_id" not in session:
        return

    team = Team.query.get(session["team_id"])
    state = AuctionState.query.first()

    amount = int(data["amount"])

    reserve, max_bid, _ = get_team_limits(team)

    if amount > max_bid or amount <= state.current_bid:
        return

    state.current_bid = amount
    state.current_team_id = team.id
    state.auction_status = "running"
    state.timer = 60

    db.session.commit()

    socketio.emit("update_bid", {
        "bid": amount,
        "team": team.team_name
    })

    socketio.start_background_task(run_timer)

# ---------------- END PLAYER ----------------
@app.route("/end_player")
def end_player():
    state = AuctionState.query.first()
    state.auction_status = "stopped"
    db.session.commit()
    return redirect("/admin/auction")

# ---------------- UNSOLD ----------------
@app.route("/unsold")
def unsold():
    state = AuctionState.query.first()

    player = Player.query.get(state.current_player_id)
    player.status = "unsold"

    socketio.emit("unsold", {"player": player.name})

    state.current_player_id = None
    state.current_bid = 0
    state.current_team_id = None
    state.auction_status = "waiting"

    db.session.commit()

    return redirect("/admin/auction")

# ---------------- UNDO LAST ----------------
@app.route("/undo")
def undo():
    last = Player.query.filter_by(status="sold").order_by(Player.id.desc()).first()

    if not last:
        return redirect("/admin/auction")

    team = Team.query.get(last.team_id)

    team.remaining_points += last.sold_price
    team.players_bought -= 1

    last.status = "unsold"
    last.team_id = None
    last.sold_price = None

    db.session.commit()

    socketio.emit("undo", {"player": last.name})

    return redirect("/admin/auction")

# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=10000)
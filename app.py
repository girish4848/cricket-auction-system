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

# ---------------- CATEGORY PRICES ----------------
CATEGORY_PRICES = {
    1: 500,
    2: 400,
    3: 300,
    4: 200,
    5: 100
}

# ---------------- INIT DB ----------------
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

# ---------------- REALTIME STATE BROADCAST ----------------
def broadcast_state():

    state = AuctionState.query.first()

    player = None
    if state.current_player_id:
        player = Player.query.get(state.current_player_id)

    teams = Team.query.all()

    socketio.emit("auction_state_update", {
        "player": {
            "name": player.name,
            "card": player.player_card,
            "base_price": player.base_price
        } if player else None,

        "bid": state.current_bid,
        "leading_team": Team.query.get(state.current_team_id).team_name
            if state.current_team_id else None,

        "timer": state.timer,
        "status": state.auction_status,

        "teams": [
            {
                "name": t.team_name,
                "points": t.remaining_points,
                "players": t.players_bought,
                "ready": t.is_ready
            } for t in teams
        ],

        "sold": [
            {
                "name": p.name,
                "team": Team.query.get(p.team_id).team_name if p.team_id else None,
                "price": p.sold_price
            }
            for p in Player.query.filter_by(status="sold").all()
        ]
    })

# ---------------- TIMER ENGINE ----------------
def run_timer():

    while True:
        state = AuctionState.query.first()

        if state.auction_status != "running":
            break

        if state.timer <= 0:

            if state.current_team_id and state.current_player_id:

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

            # reset
            state.current_player_id = None
            state.current_bid = 0
            state.current_team_id = None
            state.auction_status = "waiting"
            state.timer = 60

            db.session.commit()
            broadcast_state()
            break

        state.timer -= 1
        db.session.commit()

        broadcast_state()
        time.sleep(1)

# ---------------- HOME ----------------
@app.route("/")
def home():
    return redirect("/auction")

# ---------------- LOGIN ----------------
@app.route("/login", methods=["POST"])
def login():
    team = Team.query.filter_by(
        username=request.form["username"],
        password=request.form["password"]
    ).first()

    if team:
        session["team_id"] = team.id
        return redirect("/auction")

    return redirect("/")

# ---------------- AUCTION PAGE ----------------
@app.route("/auction")
def auction():
    if "team_id" not in session:
        return render_template("login.html")

    team = Team.query.get(session["team_id"])
    return render_template("auction.html", team=team)

# ---------------- ADMIN DASHBOARD ----------------
@app.route("/admin")
def admin():
    return render_template("admin_dashboard.html",
                           teams=Team.query.all(),
                           players=Player.query.all())

# ---------------- ADMIN AUCTION ----------------
@app.route("/admin/auction")
def admin_auction():

    state = AuctionState.query.first()

    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)

    return render_template(
        "admin_auction.html",
        players=Player.query.filter_by(status="unsold").all(),
        teams=Team.query.all(),
        state=state,
        current_player=current_player
    )

# ---------------- ADD TEAM ----------------
@app.route("/add_team", methods=["POST"])
def add_team():
    db.session.add(Team(
        team_name=request.form["team_name"],
        username=request.form["username"],
        password=request.form["password"],
        remaining_points=10000
    ))
    db.session.commit()
    return redirect("/admin")

# ---------------- ADD PLAYER ----------------
@app.route("/add_player", methods=["POST"])
def add_player():

    category = int(request.form["category"])
    card = request.files["card"]

    filename = card.filename
    card.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    player = Player(
        name=request.form["name"],
        category=category,
        base_price=CATEGORY_PRICES[category],
        player_card=filename
    )

    db.session.add(player)
    db.session.commit()

    return redirect("/admin")

# ---------------- START FULL AUCTION ----------------
@app.route("/start_full_auction")
def start_full_auction():
    state = AuctionState.query.first()
    state.full_auction_started = True
    db.session.commit()

    broadcast_state()
    return redirect("/admin/auction")

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

    broadcast_state()
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

    socketio.start_background_task(run_timer)
    broadcast_state()

# ---------------- CUSTOM BID ----------------
@socketio.on("custom_bid")
def custom_bid(data):

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

    socketio.start_background_task(run_timer)
    broadcast_state()

# ---------------- MANUAL SELL ----------------
@app.route("/manual_sell", methods=["POST"])
def manual_sell():

    player = Player.query.get(request.form["player_id"])
    team = Team.query.get(request.form["team_id"])
    amount = int(request.form["amount"])

    player.status = "sold"
    player.team_id = team.id
    player.sold_price = amount

    team.remaining_points -= amount
    team.players_bought += 1

    db.session.commit()

    socketio.emit("player_sold", {
        "player": player.name,
        "team": team.team_name,
        "price": amount
    })

    broadcast_state()
    return redirect("/admin/auction")

# ---------------- SELL CURRENT BID ----------------
@app.route("/sell_current")
def sell_current():

    state = AuctionState.query.first()

    player = Player.query.get(state.current_player_id)
    team = Team.query.get(state.current_team_id)

    if not player or not team:
        return redirect("/admin/auction")

    player.status = "sold"
    player.team_id = team.id
    player.sold_price = state.current_bid

    team.remaining_points -= state.current_bid
    team.players_bought += 1

    state.current_player_id = None
    state.current_bid = 0
    state.current_team_id = None
    state.auction_status = "waiting"
    state.timer = 60

    db.session.commit()

    socketio.emit("player_sold", {
        "player": player.name,
        "team": team.team_name,
        "price": player.sold_price
    })

    broadcast_state()
    return redirect("/admin/auction")

# ---------------- UNSOLD ----------------
@app.route("/unsold")
def unsold():

    state = AuctionState.query.first()
    player = Player.query.get(state.current_player_id)

    if player:
        player.status = "unsold"

    state.current_player_id = None
    state.current_bid = 0
    state.current_team_id = None
    state.auction_status = "waiting"
    state.timer = 60

    db.session.commit()

    broadcast_state()
    return redirect("/admin/auction")

# ---------------- REQUEST STATE ----------------
@socketio.on("request_state")
def send_state():
    broadcast_state()

# ---------------- RUN ----------------
if __name__ == "__main__":
   # socketio.run(app, host="0.0.0.0", port=10000)
    socketio.run(app, debug=True)
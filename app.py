from flask import Flask, render_template, request, redirect, session
from flask_socketio import SocketIO, emit
from models import db, Team, Player, AuctionState
import os, time

app = Flask(__name__)
app.secret_key = "auction_secret_key"

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'

db.init_app(app)
socketio = SocketIO(app,cors_allowed_origins="*", async_mode="eventlet")

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
        state = AuctionState(
            current_player_id=None,
            current_bid=0,
            current_team_id=None,
            auction_status="stopped"
        )
        db.session.add(state)
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

def start_timer():
    for i in range(10, -1, -1):
        socketio.emit("timer", {"time": i})
        time.sleep(1)
        state = AuctionState.query.first()
        if state.auction_status != "running":
            break
    socketio.emit("timer_end")

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

# ---------------- ADMIN AUCTION PAGE ----------------
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
        current_player=current_player,
        state=state
    )

# ---------------- ADD TEAM ----------------
@app.route("/add_team", methods=["POST"])
def add_team():
    team = Team(
        team_name=request.form["team_name"],
        username=request.form["username"],
        password=request.form["password"]
    )
    db.session.add(team)
    db.session.commit()
    return redirect("/admin")

# ---------------- ADD PLAYER ----------------
@app.route("/add_player", methods=["POST"])
def add_player():
    category = int(request.form["category"])
    photo = request.files["photo"]
    filename = photo.filename
    photo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    player = Player(
        name=request.form["name"],
        category=category,
        role=request.form["role"],
        description=request.form["description"],
        base_price=CATEGORY_PRICES[category],
        photo=filename
    )

    db.session.add(player)
    db.session.commit()
    return redirect("/admin")

# ---------------- AUCTION VIEW ----------------
@app.route("/auction")
def auction():
    if "team_id" not in session:
        return redirect("/")
    team = Team.query.get(session["team_id"])
    return render_template("auction.html", team=team)

# ---------------- SOCKET ----------------
@socketio.on("join")
def join():
    pass

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
    db.session.commit()

    socketio.emit("update_bid", {
    "bid": new_bid,
    "team": team.team_name,
    "team_id": team.id
    }, broadcast=True)

    socketio.start_background_task(start_timer)

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
    db.session.commit()

    socketio.emit("update_bid", {
    "bid": new_bid,
    "team": team.team_name,
    "team_id": team.id
    }, broadcast=True)

    socketio.start_background_task(start_timer)

# ---------------- START PLAYER ----------------
@app.route("/start_player/<int:pid>")
def start_player(pid):
    state = AuctionState.query.first()
    player = Player.query.get(pid)

    state.current_player_id = pid
    state.current_bid = player.base_price
    state.current_team_id = None
    state.auction_status = "running"
    db.session.commit()

    socketio.emit("new_player", {
        "name": player.name,
        "photo": player.photo,
        "role":player.role,
        "description":player.description,
        "base_price": player.base_price
    })

    socketio.start_background_task(start_timer)

    return redirect("/admin/auction")

# ---------------- SELL ----------------
@app.route("/sell_player")
def sell_player():
    state = AuctionState.query.first()
    if not state.current_team_id:
        return redirect("/admin/auction")

    team = Team.query.get(state.current_team_id)
    player = Player.query.get(state.current_player_id)

    player.team_id = team.id
    player.status = "sold"
    player.sold_price = state.current_bid

    team.remaining_points -= state.current_bid
    team.players_bought += 1

    state.current_player_id = None
    state.current_bid = 0
    state.current_team_id = None
    state.auction_status = "stopped"

    db.session.commit()

    socketio.emit("player_sold", {
    "player": player.name,
    "team": team.team_name,
    "price": state.current_bid
    }, broadcast=True)

    return redirect("/admin/auction")

@app.route("/live")
def live_board():
    teams = Team.query.all()
    players = Player.query.filter_by(status="sold").all()
    state = AuctionState.query.first()

    current_player = None
    if state.current_player_id:
        current_player = Player.query.get(state.current_player_id)

    leading_team = None
    if state.current_team_id:
        leading_team = Team.query.get(state.current_team_id)

    return render_template(
        "live_board.html",
        teams=teams,
        sold_players=players,
        current_player=current_player,
        state=state,
        leading_team=leading_team
    )
    
@app.route("/leaderboard")
def leaderboard():
    teams = Team.query.all()

    # sort by remaining points (descending)
    teams = sorted(teams, key=lambda t: t.remaining_points, reverse=True)

    return render_template("leaderboard.html", teams=teams)

# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=10000)
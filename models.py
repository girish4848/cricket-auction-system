from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Team Table
class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    team_name = db.Column(db.String(100))

    username = db.Column(db.String(100), unique=True)

    password = db.Column(db.String(100))

    total_points = db.Column(db.Integer, default=10000)

    remaining_points = db.Column(db.Integer, default=10000)

    players_bought = db.Column(db.Integer, default=0)
    
    is_ready = db.Column(db.Boolean, default=False)


# Player Table
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100))
    
    category = db.Column(db.Integer)
    
    base_price = db.Column(db.Integer)

    player_card = db.Column(db.String(200))

    status = db.Column(db.String(50), default="unsold")

    sold_price = db.Column(db.Integer)

    team_id = db.Column(db.Integer)


# Auction State
class AuctionState(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    current_player_id = db.Column(db.Integer)
    current_bid = db.Column(db.Integer)
    current_team_id = db.Column(db.Integer)

    auction_status = db.Column(db.String(50))  # stopped / running / waiting

    timer = db.Column(db.Integer, default=60)

    full_auction_started = db.Column(db.Boolean, default=False)
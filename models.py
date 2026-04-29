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


# Player Table
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100))

    category = db.Column(db.Integer)

    role = db.Column(db.String(100))
    
    description = db.Column(db.String(500))

    base_price = db.Column(db.Integer)

    photo = db.Column(db.String(200))

    status = db.Column(db.String(50), default="unsold")

    sold_price = db.Column(db.Integer)

    team_id = db.Column(db.Integer)


# Auction State
class AuctionState(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    current_player_id = db.Column(db.Integer)

    current_bid = db.Column(db.Integer)

    current_team_id = db.Column(db.Integer)

    auction_status = db.Column(db.String(50))
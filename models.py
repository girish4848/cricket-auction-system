from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_name = db.Column(db.String(100))
    username = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))
    total_points = db.Column(db.Integer, default=10000)
    remaining_points = db.Column(db.Integer, default=10000)
    players_bought = db.Column(db.Integer, default=0)
    is_ready = db.Column(db.Boolean, default=False)
    captain_photo = db.Column(db.String(255), nullable=True)
    finals_team_theme = db.Column(
        db.String(16), default="multi"
    )  # multi | blue | red | yellow — final squad export card base


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    category = db.Column(db.Integer)
    base_price = db.Column(db.Integer)
    player_card = db.Column(db.String(200))
    player_photo = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default="unsold")
    sold_price = db.Column(db.Integer)
    team_id = db.Column(db.Integer)
    eligible_for_random_pool = db.Column(db.Boolean, default=True)


class AuctionState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    current_player_id = db.Column(db.Integer)
    current_bid = db.Column(db.Integer)
    current_team_id = db.Column(db.Integer)
    auction_status = db.Column(db.String(50))
    timer = db.Column(db.Integer, default=0)
    timer_deadline = db.Column(db.Float, nullable=True)
    full_auction_started = db.Column(db.Boolean, default=False)
    last_bid_team_id = db.Column(db.Integer, nullable=True)
    auction_complete = db.Column(db.Boolean, default=False)
    finals_card_theme = db.Column(
        db.String(16), default="auto"
    )  # auto | blue | red | yellow — final squad export cards


class LastSaleUndo(db.Model):
    """Legacy single-step undo stack (superseded by SaleRecord). Kept for old DB rows."""

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, nullable=False)
    team_id = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    team_remaining_before = db.Column(db.Integer, nullable=False)
    team_players_bought_before = db.Column(db.Integer, nullable=False)


class AuctionArchive(db.Model):
    """Saved snapshot when every player is sold (auction finished)."""

    id = db.Column(db.Integer, primary_key=True)
    finished_at = db.Column(db.Float, nullable=False)
    snapshot_json = db.Column(db.Text, nullable=False)


class SaleRecord(db.Model):
    """One row per completed sale; supports undo of any sale (not only last)."""

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, nullable=False)
    team_id = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    team_remaining_before = db.Column(db.Integer, nullable=False)
    team_players_bought_before = db.Column(db.Integer, nullable=False)
    undone = db.Column(db.Boolean, default=False)
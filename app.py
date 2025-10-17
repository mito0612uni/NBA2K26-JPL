import os
import random
import string
import cloudinary
import cloudinary.uploader
import cloudinary.api
import re
import io
import json
import sys
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, case, or_
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from functools import wraps
from collections import defaultdict, deque
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from itertools import combinations
from PIL import Image, ImageEnhance

# --- 1. アプリケーションとデータベースの初期設定 ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_very_secret_key_change_it'
basedir = os.path.abspath(os.path.dirname(__file__))

cloudinary.config( 
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'), 
    api_key = os.environ.get('CLOUDINARY_API_KEY'), 
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)

database_url = os.environ.get('DATABASE_URL')
if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url.replace("postgres://", "postgresql://", 1)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- 2. ログインマネージャーの設定 ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "このページにアクセスするにはログインが必要です。"

# --- 3. データベースモデル（テーブル）の定義 ---
# (User, Team, Player, Game, PlayerStat モデルの定義は変更なし)
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), nullable=False, default='user')
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    @property
    def is_admin(self): return self.role == 'admin'

class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    logo_image = db.Column(db.String(255), nullable=True)
    league = db.Column(db.String(50), nullable=True)
    players = db.relationship('Player', backref='team', lazy=True)

class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_date = db.Column(db.String(50))
    start_time = db.Column(db.String(20), nullable=True)
    game_password = db.Column(db.String(50), nullable=True)
    home_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    away_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    home_score = db.Column(db.Integer, default=0)
    away_score = db.Column(db.Integer, default=0)
    is_finished = db.Column(db.Boolean, default=False)
    youtube_url_home = db.Column(db.String(200), nullable=True)
    youtube_url_away = db.Column(db.String(200), nullable=True)
    winner_id = db.Column(db.Integer, nullable=True)
    loser_id = db.Column(db.Integer, nullable=True)
    home_team = db.relationship('Team', foreign_keys=[home_team_id])
    away_team = db.relationship('Team', foreign_keys=[away_team_id])

class PlayerStat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    pts=db.Column(db.Integer, default=0); ast=db.Column(db.Integer, default=0)
    reb=db.Column(db.Integer, default=0); stl=db.Column(db.Integer, default=0)
    blk=db.Column(db.Integer, default=0); foul=db.Column(db.Integer, default=0)
    turnover=db.Column(db.Integer, default=0); fgm=db.Column(db.Integer, default=0)
    fga=db.Column(db.Integer, default=0); three_pm=db.Column(db.Integer, default=0)
    three_pa=db.Column(db.Integer, default=0); ftm=db.Column(db.Integer, default=0)
    fta=db.Column(db.Integer, default=0)
    player = db.relationship('Player')
# --- 4. 権限管理とヘルパー関数 ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("この操作には管理者権限が必要です。"); return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif'}

def generate_password(length=4):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ★★★ ここに preprocess_image 関数を追加 ★★★
def preprocess_image(image_stream):
    """画像をOCRで読みやすいように前処理する"""
    img = Image.open(image_stream)
    img = img.convert('L')
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

def parse_nba2k_stats(ocr_text):
    # (この関数は変更なし)
    print("--- OCR RAW TEXT (Final, Skip-Total Method) ---"); print(ocr_text); sys.stdout.flush()
    tokens = ocr_text.split()
    header_map = {'PTS': 'pts', 'REB': 'reb', 'AST': 'ast', 'STL': 'stl', 'BLK': 'blk', 'FOULS': 'foul', 'TO': 'turnover'}
    fraction_header_map = {'FGM/FGA': ('fgm', 'fga'), '3PM/3PA': ('three_pm', 'three_pa'), 'FTM/FTA': ('ftm', 'fta')}
    num_players = 10
    player_stats = [{} for _ in range(num_players)]
    print("--- PARSING TOKENS BY COLUMN (SKIPPING TOTALS) ---"); sys.stdout.flush()
    for header, key in header_map.items():
        try:
            start_index = tokens.index(header)
            team1_stats = tokens[start_index + 1 : start_index + 6]; team2_stats = tokens[start_index + 7 : start_index + 12]
            stats_for_this_column = team1_stats + team2_stats
            if len(stats_for_this_column) != 10: continue
            print(f"Found column '{header}', data: {stats_for_this_column}"); sys.stdout.flush()
            for i in range(num_players):
                stat_value = stats_for_this_column[i] if stats_for_this_column[i].isdigit() else '0'
                player_stats[i][key] = int(stat_value)
        except (ValueError, IndexError): continue
    for header, (key1, key2) in fraction_header_map.items():
        try:
            start_index = tokens.index(header)
            team1_stats = tokens[start_index + 1 : start_index + 6]; team2_stats = tokens[start_index + 7 : start_index + 12]
            stats_for_this_column = team1_stats + team2_stats
            if len(stats_for_this_column) != 10: continue
            print(f"Found column '{header}', data: {stats_for_this_column}"); sys.stdout.flush()
            for i in range(num_players):
                parts = stats_for_this_column[i].split('/')
                if len(parts) == 2: player_stats[i][key1] = int(parts[0]); player_stats[i][key2] = int(parts[1])
        except (ValueError, IndexError): continue
    final_stats_list = [stats for stats in player_stats if len(stats) >= 12]
    print(f"--- PARSED {len(final_stats_list)} STATS BLOCKS ---"); print(final_stats_list); sys.stdout.flush()
    return final_stats_list

# --- 5. ルート（ページの表示と処理） ---
# ... (calculate_standings, get_stats_leaders, calculate_team_stats, login, logout, register, index, roster, schedule, add_schedule, auto_schedule, delete_team, delete_player, delete_game, forfeit_game, edit_game, stats_page などの関数は変更なし) ...

@app.route('/')
def index():
    overall_standings = calculate_standings()
    league_a_standings = calculate_standings(league_filter="Aリーグ")
    league_b_standings = calculate_standings(league_filter="Bリーグ")
    stats_leaders = get_stats_leaders()
    upcoming_games = Game.query.filter_by(is_finished=False).order_by(Game.game_date.asc(), Game.start_time.asc()).all()
    
    # ロゴ表示のために全チーム情報を渡す
    teams_data = Team.query.all()
    
    return render_template('index.html', 
                           overall_standings=overall_standings,
                           league_a_standings=league_a_standings,
                           league_b_standings=league_b_standings,
                           leaders=stats_leaders, 
                           upcoming_games=upcoming_games,
                           teams_data=teams_data)

@app.route('/ocr-upload', methods=['POST'])
@login_required
@admin_required
def ocr_upload():
    if 'image' not in request.files:
        return jsonify({'error': '画像ファイルがありません'}), 400
    file = request.files['image']
    if not (file and file.filename != '' and allowed_file(file.filename)):
        return jsonify({'error': 'ファイルが選択されていないか、形式が不正です'}), 400
    try:
        api_key = os.environ.get('OCR_SPACE_API_KEY')
        if not api_key: raise Exception("OCR.spaceのAPIキーが設定されていません。")
        
        processed_image = preprocess_image(file.stream)
        payload = {'isOverlayRequired': False, 'apikey': api_key, 'language': 'eng', 'OcrEngine': 2}
        
        response = requests.post(
            'https://api.ocr.space/parse/image',
            files={'file': ('image.png', processed_image, 'image/png')},
            data=payload,
        )
        response.raise_for_status()
        result = response.json()

        if not result.get('ParsedResults'):
            return jsonify({'error': f"OCR APIからのエラー: {result.get('ErrorMessage', '不明なエラー')}"}), 500
        
        full_text = result['ParsedResults'][0]['ParsedText']
        parsed_data = parse_nba2k_stats(full_text)
        
        if not parsed_data:
            return jsonify({'error': '画像から有効なスタッツを見つけられませんでした。'}), 500
        
        return jsonify(parsed_data)

    except Exception as e:
        return jsonify({'error': f'OCR処理中にエラーが発生しました: {str(e)}'}), 500

# --- 6. データベース初期化コマンドと実行 ---
@app.cli.command('init-db')
def init_db_command():
    db.drop_all()
    db.create_all()
    print('Initialized the database.')

if __name__ == '__main__':
    app.run(debug=True)
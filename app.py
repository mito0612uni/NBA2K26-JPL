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
from PIL import Image, ImageEnhance, ImageFilter

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

def preprocess_image(image_stream):
    img = Image.open(image_stream)
    img = img.convert('L')
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    enhancer_sharpness = ImageEnhance.Sharpness(img)
    img = enhancer_sharpness.enhance(2.0)
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

def parse_nba2k_stats(ocr_text):
    print("--- OCR RAW TEXT (Final, Definitive Method) ---"); print(ocr_text); sys.stdout.flush()
    tokens = ocr_text.split()
    header_map = {
        'GRD': 'name', 'PTS': 'pts', 'REB': 'reb', 'AST': 'ast', 'STL': 'stl', 
        'BLK': 'blk', 'FOULS': 'foul', 'TO': 'turnover',
        'FGM/FGA': 'fgm/fga', '3PM/3PA': '3pm/3pa', 'FTM/FTA': 'ftm/fta'
    }
    columns = {}
    all_headers = list(header_map.keys())
    print("--- PARSING TOKENS BY COLUMN (DEFINITIVE) ---"); sys.stdout.flush()
    header_indices = {h: [i for i, t in enumerate(tokens) if t == h] for h in all_headers}
    for header, key in header_map.items():
        indices = header_indices.get(header)
        if not indices: continue
        temp_col_data = []
        for header_index in indices:
            next_header_pos = len(tokens)
            for h_key in all_headers:
                for idx in header_indices.get(h_key, []):
                    if idx > header_index and idx < next_header_pos:
                        next_header_pos = idx
            raw_column_data = tokens[header_index + 1 : next_header_pos]
            if key in ['fgm/fga', '3pm/3pa', 'ftm/fta']:
                cleaned_data = [t for t in raw_column_data if re.match(r'^\d+/\d+$', t)]
            elif key == 'name':
                 cleaned_data = [t for t in raw_column_data if re.match(r'^[a-zA-Z0-9_-]{3,}$', t.lstrip('O©•@‣▸'))]
            else:
                cleaned_data = [t for t in raw_column_data if t.isdigit()]
            temp_col_data.extend(cleaned_data)
        if len(temp_col_data) >= 11:
            team1_data = temp_col_data[0:5]
            team2_data = temp_col_data[6:11]
            columns[key] = team1_data + team2_data
            print(f"Found and Cleaned column '{header}', data: {columns[key]}"); sys.stdout.flush()
    final_player_list = []
    if not columns or not all(columns.get(key) and len(columns[key]) == 10 for key in header_map.values()):
        print("--- PARSING FAILED: Incomplete columns ---"); sys.stdout.flush()
        return []
    for i in range(10):
        try:
            player_name = columns['name'][i].lstrip('O©•@‣▸')
            fgm, fga = map(int, columns['fgm/fga'][i].split('/'))
            three_pm, three_pa = map(int, columns['3pm/3pa'][i].split('/'))
            ftm, fta = map(int, columns['ftm/fta'][i].split('/'))
            player_data = {
                'name': player_name,
                'stats': {
                    'pts': int(columns['pts'][i]), 'reb': int(columns['reb'][i]), 'ast': int(columns['ast'][i]),
                    'stl': int(columns['stl'][i]), 'blk': int(columns['blk'][i]), 'foul': int(columns['foul'][i]),
                    'turnover': int(columns['to'][i]), 'fgm': fgm, 'fga': fga,
                    'three_pm': three_pm, 'three_pa': three_pa, 'ftm': ftm, 'fta': fta
                }
            }
            final_player_list.append(player_data)
        except (KeyError, ValueError, IndexError) as e:
            print(f"Skipping player data at index {i} due to parsing error: {e}"); sys.stdout.flush(); continue
    print(f"--- PARSED {len(final_player_list)} PLAYERS ---"); print(final_player_list); sys.stdout.flush()
    return final_player_list

def calculate_standings(league_filter=None):
    # ... (変更なし)
    
def get_stats_leaders():
    # ... (変更なし)

def calculate_team_stats():
    # ... (変更なし)

# --- 5. ルート（ページの表示と処理） ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    # ... (変更なし)

@app.route('/logout')
@login_required
def logout():
    # ... (変更なし)

@app.route('/register', methods=['GET', 'POST'])
def register():
    # ... (変更なし)

@app.route('/')
def index():
    overall_standings = calculate_standings()
    league_a_standings = calculate_standings(league_filter="Aリーグ")
    league_b_standings = calculate_standings(league_filter="Bリーグ")
    stats_leaders = get_stats_leaders()
    upcoming_games = Game.query.filter_by(is_finished=False).order_by(Game.game_date.asc(), Game.start_time.asc()).all()
    return render_template('index.html', overall_standings=overall_standings,
                           league_a_standings=league_a_standings, league_b_standings=league_b_standings,
                           leaders=stats_leaders, upcoming_games=upcoming_games)

@app.route('/roster', methods=['GET', 'POST'])
@login_required
@admin_required
def roster():
    # ... (変更なし)

@app.route('/schedule')
def schedule():
    # ... (変更なし)

@app.route('/add_schedule', methods=['GET', 'POST'])
@login_required
@admin_required
def add_schedule():
    # ... (変更なし)

@app.route('/auto_schedule', methods=['GET', 'POST'])
@login_required
@admin_required
def auto_schedule():
    # ... (変更なし)

@app.route('/team/delete/<int:team_id>', methods=['POST'])
@login_required
@admin_required
def delete_team(team_id):
    # ... (変更なし)

@app.route('/player/delete/<int:player_id>', methods=['POST'])
@login_required
@admin_required
def delete_player(player_id):
    # ... (変更なし)

@app.route('/game/delete/<int:game_id>', methods=['POST'])
@login_required
@admin_required
def delete_game(game_id):
    # ... (変更なし)

@app.route('/game/<int:game_id>/forfeit', methods=['POST'])
@login_required
@admin_required
def forfeit_game(game_id):
    # ... (変更なし)

@app.route('/game/<int:game_id>/edit', methods=['GET', 'POST'])
def edit_game(game_id):
    # ... (変更なし)

@app.route('/stats')
def stats_page():
    # ... (変更なし)

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
import os
import random
import string
import cloudinary
import cloudinary.uploader
import cloudinary.api
import re
import io
import json
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
from google.cloud import vision

# --- 1. アプリケーションとデータベースの初期設定 ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_very_secret_key_change_it'
# RenderのSecret Fileのパスを設定
google_credentials_path = '/etc/secrets/google_credentials.json'
if os.path.exists(google_credentials_path):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = google_credentials_path

basedir = os.path.abspath(os.path.dirname(__file__))

# Cloudinary設定
cloudinary.config( 
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'), 
    api_key = os.environ.get('CLOUDINARY_API_KEY'), 
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)

# データベース設定
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

def calculate_standings(league_filter=None):
    if league_filter: teams = Team.query.filter_by(league=league_filter).all()
    else: teams = Team.query.all()
    standings = []
    for team in teams:
        wins, losses, points_for, points_against, stats_games_played = 0, 0, 0, 0, 0
        home_games = Game.query.filter_by(home_team_id=team.id, is_finished=True).all()
        for game in home_games:
            if game.winner_id is None:
                points_for += game.home_score; points_against += game.away_score; stats_games_played += 1
            if game.winner_id == team.id: wins += 1
            elif game.loser_id == team.id: losses += 1
            elif game.home_score > game.away_score: wins += 1
            elif game.home_score < game.away_score: losses += 1
        away_games = Game.query.filter_by(away_team_id=team.id, is_finished=True).all()
        for game in away_games:
            if game.winner_id is None:
                points_for += game.away_score; points_against += game.home_score; stats_games_played += 1
            if game.winner_id == team.id: wins += 1
            elif game.loser_id == team.id: losses += 1
            elif game.away_score > game.home_score: wins += 1
            elif game.away_score < game.home_score: losses += 1
        points = (wins * 2) + (losses * 1)
        standings.append({
            'team': team, 'team_name': team.name, 'league': team.league, 'wins': wins, 'losses': losses, 'points': points,
            'avg_pf': round(points_for / stats_games_played, 1) if stats_games_played > 0 else 0,
            'avg_pa': round(points_against / stats_games_played, 1) if stats_games_played > 0 else 0,
            'diff': points_for - points_against, 'stats_games_played': stats_games_played
        })
    standings.sort(key=lambda x: (x['points'], x['diff']), reverse=True)
    return standings

def get_stats_leaders():
    leaders = {}
    stat_fields = {'pts': '平均得点', 'ast': '平均アシスト', 'reb': '平均リバウンド', 'stl': '平均スティール', 'blk': '平均ブロック'}
    for field_key, field_name in stat_fields.items():
        avg_stat = func.avg(getattr(PlayerStat, field_key)).label('avg_value')
        query_result = db.session.query(Player.name, avg_stat).join(PlayerStat, PlayerStat.player_id == Player.id).group_by(Player.id).order_by(db.desc('avg_value')).limit(5).all()
        leaders[field_name] = query_result
    return leaders

def calculate_team_stats():
    team_stats_list = []
    standings_info = calculate_standings()
    shooting_stats_query = db.session.query(
        Player.team_id, func.sum(PlayerStat.pts).label('total_pts'),
        func.sum(PlayerStat.ast).label('total_ast'), func.sum(PlayerStat.reb).label('total_reb'),
        func.sum(PlayerStat.stl).label('total_stl'), func.sum(PlayerStat.blk).label('total_blk'),
        func.sum(PlayerStat.foul).label('total_foul'), func.sum(PlayerStat.turnover).label('total_turnover'),
        func.sum(PlayerStat.fgm).label('total_fgm'), func.sum(PlayerStat.fga).label('total_fga'),
        func.sum(PlayerStat.three_pm).label('total_3pm'), func.sum(PlayerStat.three_pa).label('total_3pa'),
        func.sum(PlayerStat.ftm).label('total_ftm'), func.sum(PlayerStat.fta).label('total_fta')
    ).join(Player).group_by(Player.team_id).all()
    shooting_map = {s.team_id: s for s in shooting_stats_query}
    for team_standings in standings_info:
        team_obj = team_standings.get('team')
        if not team_obj: continue
        stats_games_played = team_standings.get('stats_games_played', 0)
        team_shooting = shooting_map.get(team_obj.id)
        stats_dict = team_standings.copy()
        if stats_games_played > 0 and team_shooting:
            stats_dict.update({
                'avg_ast': team_shooting.total_ast / stats_games_played, 'avg_reb': team_shooting.total_reb / stats_games_played,
                'avg_stl': team_shooting.total_stl / stats_games_played, 'avg_blk': team_shooting.total_blk / stats_games_played,
                'avg_foul': team_shooting.total_foul / stats_games_played, 'avg_turnover': team_shooting.total_turnover / stats_games_played,
                'avg_fgm': team_shooting.total_fgm / stats_games_played, 'avg_fga': team_shooting.total_fga / stats_games_played,
                'avg_three_pm': team_shooting.total_3pm / stats_games_played, 'avg_three_pa': team_shooting.total_3pa / stats_games_played,
                'avg_ftm': team_shooting.total_ftm / stats_games_played, 'avg_fta': team_shooting.total_fta / stats_games_played,
                'fg_pct': (team_shooting.total_fgm / team_shooting.total_fga * 100) if team_shooting.total_fga > 0 else 0,
                'three_p_pct': (team_shooting.total_3pm / team_shooting.total_3pa * 100) if team_shooting.total_3pa > 0 else 0,
                'ft_pct': (team_shooting.total_ftm / team_shooting.total_fta * 100) if team_shooting.total_fta > 0 else 0,
            })
        team_stats_list.append(stats_dict)
    return team_stats_list

def parse_nba2k_stats(text):
    """テキストからスタッツの数値ブロックだけを順番に抽出する関数"""
    print("--- OCR RAW TEXT (Final Method) ---")
    print(text)
    sys.stdout.flush()

    found_stats_blocks = []
    
    # 「グレード(A+) + 7つの数字 + 3つの分数」というパターンを探す
    # 数字間のスペースは0個以上(\s*)を許容し、柔軟性を持たせる
    pattern = re.compile(
        r'[A-Z][+-]?\s*'
        r'(\d+)\s*(\d+)\s*(\d+)\s*(\d+)\s*(\d+)\s*(\d+)\s*(\d+)\s*'
        r'(\d+/\d+)\s*(\d+/\d+)\s*(\d+/\d+)'
    )

    print("--- FINDING STATS BLOCKS ---")
    sys.stdout.flush()

    # findall を使って、テキスト中にある全てのスタッツブロックを順番に取得
    matches = pattern.findall(text)
    
    for match_group in matches:
        print(f"FOUND BLOCK: {match_group}")
        sys.stdout.flush()
        try:
            # FGM/FGAなどを分割
            fgm_fga_str, three_pm_pa_str, ftm_fta_str = match_group[7], match_group[8], match_group[9]
            fgm, fga = map(int, fgm_fga_str.split('/'))
            three_pm, three_pa = map(int, three_pm_pa_str.split('/'))
            ftm, fta = map(int, ftm_fta_str.split('/'))

            found_stats_blocks.append({
                'pts': int(match_group[0]), 'reb': int(match_group[1]), 'ast': int(match_group[2]),
                'stl': int(match_group[3]), 'blk': int(match_group[4]), 'foul': int(match_group[5]),
                'turnover': int(match_group[6]), 'fgm': fgm, 'fga': fga,
                'three_pm': three_pm, 'three_pa': three_pa, 'ftm': ftm, 'fta': fta
            })
        except (ValueError, IndexError) as e:
            print(f"Failed to parse a block: {match_group}, Error: {e}")
            sys.stdout.flush()
            continue
            
    print(f"--- PARSED {len(found_stats_blocks)} STATS BLOCKS ---")
    sys.stdout.flush()
    
    return found_stats_blocks# --- 5. ルート（ページの表示と処理） ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user is None or not user.check_password(request.form['password']):
            flash('ユーザー名またはパスワードが無効です'); return redirect(url_for('login'))
        login_user(user); return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user(); flash('ログアウトしました。'); return redirect(url_for('index'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        if User.query.filter_by(username=username).first():
            flash("そのユーザー名は既に使用されています。"); return redirect(url_for('register'))
        role = 'admin' if User.query.count() == 0 else 'user'
        new_user = User(username=username, role=role)
        new_user.set_password(request.form['password'])
        db.session.add(new_user); db.session.commit()
        flash(f"ユーザー登録が完了しました。ログインしてください。"); return redirect(url_for('login'))
    return render_template('register.html')

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
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_team':
            team_name = request.form.get('team_name'); league = request.form.get('league')
            logo_url = None
            if 'logo_image' in request.files:
                file = request.files['logo_image']
                if file and file.filename != '' and allowed_file(file.filename):
                    try:
                        upload_result = cloudinary.uploader.upload(file)
                        logo_url = upload_result.get('secure_url')
                    except Exception as e:
                        flash(f"画像アップロードに失敗しました: {e}"); return redirect(url_for('roster'))
                elif file.filename != '': flash('許可されていないファイル形式です。'); return redirect(url_for('roster'))
            if team_name and league:
                if not Team.query.filter_by(name=team_name).first():
                    new_team = Team(name=team_name, league=league, logo_image=logo_url)
                    db.session.add(new_team); db.session.commit()
                    flash(f'チーム「{team_name}」が{league}に登録されました。')
                    for i in range(1, 11):
                        player_name = request.form.get(f'player_name_{i}')
                        if player_name:
                            new_player = Player(name=player_name, team_id=new_team.id)
                            db.session.add(new_player)
                    db.session.commit()
                else: flash(f'チーム「{team_name}」は既に存在します。')
            else: flash('チーム名とリーグを選択してください。')
        elif action == 'add_player':
            player_name = request.form.get('player_name'); team_id = request.form.get('team_id')
            if player_name and team_id:
                new_player = Player(name=player_name, team_id=team_id)
                db.session.add(new_player); db.session.commit()
                flash(f'選手「{player_name}」が登録されました。')
            else: flash('選手名とチームを選択してください。')
        elif action == 'promote_user':
            username_to_promote = request.form.get('username_to_promote')
            if username_to_promote:
                user_to_promote = User.query.filter_by(username=username_to_promote).first()
                if user_to_promote:
                    if user_to_promote.role != 'admin':
                        user_to_promote.role = 'admin'; db.session.commit()
                        flash(f'ユーザー「{username_to_promote}」を管理者に昇格させました。')
                    else: flash(f'ユーザー「{username_to_promote}」は既に管理者です。')
                else: flash(f'ユーザー「{username_to_promote}」が見つかりません。')
            else: flash('ユーザー名を入力してください。')
        elif action == 'edit_player':
            player_id = request.form.get('player_id', type=int); new_name = request.form.get('new_name')
            player = Player.query.get(player_id)
            if player and new_name:
                player.name = new_name; db.session.commit()
                flash(f'選手名を「{new_name}」に変更しました。')
        elif action == 'transfer_player':
            player_id = request.form.get('player_id', type=int); new_team_id = request.form.get('new_team_id', type=int)
            player = Player.query.get(player_id); new_team = Team.query.get(new_team_id)
            if player and new_team:
                old_team_name = player.team.name
                player.team_id = new_team_id; db.session.commit()
                flash(f'選手「{player.name}」を{old_team_name}から{new_team.name}に移籍させました。')
        return redirect(url_for('roster'))
    teams = Team.query.all(); users = User.query.all()
    return render_template('roster.html', teams=teams, users=users)

@app.route('/schedule')
def schedule():
    team_id = request.args.get('team_id', type=int)
    query = Game.query
    if team_id:
        query = query.filter(or_(Game.home_team_id == team_id, Game.away_team_id == team_id))
    games = query.order_by(Game.game_date.asc(), Game.start_time.asc()).all()
    all_teams = Team.query.order_by(Team.name).all()
    return render_template('schedule.html', games=games, all_teams=all_teams, selected_team_id=team_id)

@app.route('/add_schedule', methods=['GET', 'POST'])
@login_required
@admin_required
def add_schedule():
    if request.method == 'POST':
        game_date = request.form['game_date']; start_time = request.form['start_time']
        home_team_id = request.form['home_team_id']; away_team_id = request.form['away_team_id']
        game_password = request.form.get('game_password')
        if home_team_id == away_team_id:
            flash("ホームチームとアウェイチームは同じチームを選択できません。"); return redirect(url_for('add_schedule'))
        new_game = Game(game_date=game_date, start_time=start_time, home_team_id=home_team_id, away_team_id=away_team_id, game_password=game_password)
        db.session.add(new_game); db.session.commit()
        flash("新しい試合日程が追加されました。"); return redirect(url_for('schedule'))
    teams = Team.query.all()
    return render_template('add_schedule.html', teams=teams)

@app.route('/auto_schedule', methods=['GET', 'POST'])
@login_required
@admin_required
def auto_schedule():
    if request.method == 'POST':
        start_date_str = request.form.get('start_date'); weekdays = request.form.getlist('weekdays'); times_str = request.form.get('times')
        if not all([start_date_str, weekdays, times_str]):
            flash('すべての項目を入力してください。'); return redirect(url_for('auto_schedule'))
        teams = list(Team.query.all())
        if len(teams) < 2:
            flash('対戦するには少なくとも2チーム必要です。'); return redirect(url_for('auto_schedule'))
        if len(teams) % 2 != 0: teams.append(None)
        num_teams = len(teams); num_rounds = num_teams - 1
        all_rounds = []; rotating_teams = deque(teams[1:])
        for _ in range(num_rounds):
            round_matchups = []; round_matchups.append((teams[0], rotating_teams[-1]))
            for i in range((num_teams // 2) - 1):
                round_matchups.append((rotating_teams[i], rotating_teams[-(i + 2)]))
            all_rounds.append(round_matchups); rotating_teams.rotate(1)
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        selected_weekdays = [int(d) for d in weekdays]; times = [t.strip() for t in times_str.split(',')]
        total_slots_needed = len(all_rounds); time_slots = []; current_date = start_date
        while len(time_slots) < total_slots_needed:
            if current_date.weekday() in selected_weekdays:
                for time_slot in times:
                    if len(time_slots) < total_slots_needed:
                         time_slots.append({'date': current_date.strftime('%Y-%m-%d'), 'time': time_slot})
            current_date += timedelta(days=1)
        num_games_per_slot = num_teams // 2; alphabet = 'abcdefghijklmnopqrstuvwxyz'
        passwords_for_slot = [(alphabet[i % len(alphabet)] * 4) for i in range(num_games_per_slot)]
        games_created_count = 0
        for round_index, matchups_in_round in enumerate(all_rounds):
            slot = time_slots[round_index]
            for match_index, match in enumerate(matchups_in_round):
                home_team, away_team = match
                if home_team is None or away_team is None: continue
                game_password = passwords_for_slot[match_index]
                new_game = Game(game_date=slot['date'], start_time=slot['time'],
                                home_team_id=home_team.id, away_team_id=away_team.id, game_password=game_password)
                db.session.add(new_game); games_created_count += 1
        db.session.commit()
        flash(f'{games_created_count}試合の総当たり日程を自動作成しました。'); return redirect(url_for('schedule'))
    return render_template('auto_schedule.html')

@app.route('/team/delete/<int:team_id>', methods=['POST'])
@login_required
@admin_required
def delete_team(team_id):
    team_to_delete = Team.query.get_or_404(team_id)
    if team_to_delete.logo_image:
        try:
            public_id = os.path.splitext(team_to_delete.logo_image.split('/')[-1])[0]
            cloudinary.uploader.destroy(public_id)
        except Exception as e:
            print(f"Cloudinary image deletion failed: {e}")
    Player.query.filter_by(team_id=team_id).delete()
    games_to_delete = Game.query.filter(or_(Game.home_team_id==team_id, Game.away_team_id==team_id)).all()
    for game in games_to_delete:
        PlayerStat.query.filter_by(game_id=game.id).delete()
        db.session.delete(game)
    db.session.delete(team_to_delete); db.session.commit()
    flash(f'チーム「{team_to_delete.name}」と関連データを全て削除しました。'); return redirect(url_for('roster'))

@app.route('/player/delete/<int:player_id>', methods=['POST'])
@login_required
@admin_required
def delete_player(player_id):
    player_to_delete = Player.query.get_or_404(player_id)
    player_name = player_to_delete.name
    PlayerStat.query.filter_by(player_id=player_id).delete()
    db.session.delete(player_to_delete); db.session.commit()
    flash(f'選手「{player_name}」と関連スタッツを削除しました。'); return redirect(url_for('roster'))

@app.route('/game/delete/<int:game_id>', methods=['POST'])
@login_required
@admin_required
def delete_game(game_id):
    game_to_delete = Game.query.get_or_404(game_id)
    PlayerStat.query.filter_by(game_id=game_id).delete()
    db.session.delete(game_to_delete); db.session.commit()
    flash('試合日程を削除しました。'); return redirect(url_for('schedule'))

@app.route('/game/<int:game_id>/forfeit', methods=['POST'])
@login_required
@admin_required
def forfeit_game(game_id):
    game = Game.query.get_or_404(game_id); winning_team_id = request.form.get('winning_team_id', type=int)
    if winning_team_id == game.home_team_id:
        game.winner_id = game.home_team_id; game.loser_id = game.away_team_id
    elif winning_team_id == game.away_team_id:
        game.winner_id = game.away_team_id; game.loser_id = game.home_team_id
    else: flash('無効なチームが選択されました。'); return redirect(url_for('edit_game', game_id=game_id))
    game.is_finished = True; game.home_score = 0; game.away_score = 0
    PlayerStat.query.filter_by(game_id=game_id).delete()
    db.session.commit()
    flash('不戦勝として試合結果を記録しました。'); return redirect(url_for('schedule'))

@app.route('/game/<int:game_id>/edit', methods=['GET', 'POST'])
def edit_game(game_id):
    game = Game.query.get_or_404(game_id)
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('結果を保存するにはログインが必要です。'); return redirect(url_for('login'))
        game.youtube_url_home = request.form.get('youtube_url_home'); game.youtube_url_away = request.form.get('youtube_url_away')
        PlayerStat.query.filter_by(game_id=game_id).delete()
        home_total_score, away_total_score = 0, 0
        for team in [game.home_team, game.away_team]:
            for player in team.players:
                if f'player_{player.id}_pts' in request.form:
                    stat = PlayerStat(game_id=game.id, player_id=player.id); db.session.add(stat)
                    stat.pts = request.form.get(f'player_{player.id}_pts', 0, type=int); stat.ast = request.form.get(f'player_{player.id}_ast', 0, type=int)
                    stat.reb = request.form.get(f'player_{player.id}_reb', 0, type=int); stat.stl = request.form.get(f'player_{player.id}_stl', 0, type=int)
                    stat.blk = request.form.get(f'player_{player.id}_blk', 0, type=int); stat.foul = request.form.get(f'player_{player.id}_foul', 0, type=int)
                    stat.turnover = request.form.get(f'player_{player.id}_turnover', 0, type=int); stat.fgm = request.form.get(f'player_{player.id}_fgm', 0, type=int)
                    stat.fga = request.form.get(f'player_{player.id}_fga', 0, type=int); stat.three_pm = request.form.get(f'player_{player.id}_three_pm', 0, type=int)
                    stat.three_pa = request.form.get(f'player_{player.id}_three_pa', 0, type=int); stat.ftm = request.form.get(f'player_{player.id}_ftm', 0, type=int)
                    stat.fta = request.form.get(f'player_{player.id}_fta', 0, type=int)
                    if team.id == game.home_team_id: home_total_score += stat.pts
                    else: away_total_score += stat.pts
        game.home_score = home_total_score; game.away_score = away_total_score
        game.is_finished = True; game.winner_id = None; game.loser_id = None
        db.session.commit()
        flash('試合結果が更新されました。'); return redirect(url_for('schedule'))
    stats = {str(stat.player_id): {'pts': stat.pts, 'ast': stat.ast, 'reb': stat.reb, 'stl': stat.stl, 'blk': stat.blk, 'foul': stat.foul, 'turnover': stat.turnover, 'fgm': stat.fgm, 'fga': stat.fga, 'three_pm': stat.three_pm, 'three_pa': stat.three_pa, 'ftm': stat.ftm, 'fta': stat.fta} for stat in PlayerStat.query.filter_by(game_id=game_id).all()}
    return render_template('game_edit.html', game=game, stats=stats)

@app.route('/stats')
def stats_page():
    team_stats = calculate_team_stats()
    games_played = func.count(PlayerStat.game_id).label('games_played')
    avg_pts=func.avg(PlayerStat.pts).label('avg_pts'); avg_ast=func.avg(PlayerStat.ast).label('avg_ast')
    avg_reb=func.avg(PlayerStat.reb).label('avg_reb'); avg_stl=func.avg(PlayerStat.stl).label('avg_stl')
    avg_blk=func.avg(PlayerStat.blk).label('avg_blk'); avg_foul=func.avg(PlayerStat.foul).label('avg_foul')
    avg_turnover=func.avg(PlayerStat.turnover).label('avg_turnover'); avg_fgm=func.avg(PlayerStat.fgm).label('avg_fgm')
    avg_fga=func.avg(PlayerStat.fga).label('avg_fga'); avg_three_pm=func.avg(PlayerStat.three_pm).label('avg_three_pm')
    avg_three_pa=func.avg(PlayerStat.three_pa).label('avg_three_pa'); avg_ftm=func.avg(PlayerStat.ftm).label('avg_ftm')
    avg_fta=func.avg(PlayerStat.fta).label('avg_fta')
    total_fgm = func.sum(PlayerStat.fgm); total_fga = func.sum(PlayerStat.fga)
    fg_percentage = case((total_fga > 0, (total_fgm * 100.0 / total_fga)), else_=0).label('fg_pct')
    total_3pm = func.sum(PlayerStat.three_pm); total_3pa = func.sum(PlayerStat.three_pa)
    three_p_percentage = case((total_3pa > 0, (total_3pm * 100.0 / total_3pa)), else_=0).label('three_p_pct')
    total_ftm = func.sum(PlayerStat.ftm); total_fta = func.sum(PlayerStat.fta)
    ft_percentage = case((total_fta > 0, (total_ftm * 100.0 / total_fta)), else_=0).label('ft_pct')
    individual_stats = db.session.query(
        Player.name.label('player_name'), Team.name.label('team_name'), games_played,
        avg_pts, avg_ast, avg_reb, avg_stl, avg_blk, avg_foul, avg_turnover,
        avg_fgm, avg_fga, avg_three_pm, avg_three_pa, avg_ftm, avg_fta,
        fg_percentage, three_p_percentage, ft_percentage
    ).join(Player, PlayerStat.player_id == Player.id).join(Team, Player.team_id == Team.id).group_by(Player.id, Team.name).all()
    return render_template('stats.html', team_stats=team_stats, individual_stats=individual_stats)

# --- 6. データベース初期化コマンドと実行 ---
@app.cli.command('init-db')
def init_db_command():
    db.drop_all()
    db.create_all()
    print('Initialized the database.')

if __name__ == '__main__':
    app.run(debug=True)

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
        client = vision.ImageAnnotatorClient()
        content = file.read()
        image = vision.Image(content=content)
        
        response = client.text_detection(image=image)
        if response.error.message: raise Exception(response.error.message)
        
        # 最初の注釈（画像全体のテキスト）を取得
        full_text = ""
        if response.text_annotations:
            full_text = response.text_annotations[0].description
        
        # 解析関数はテキストだけを受け取る
        parsed_data = parse_nba2k_stats(full_text)
        
        if not parsed_data:
            return jsonify({'error': '画像から有効なスタッツの組み合わせを見つけられませんでした。'}), 500

        # 解析結果のリストをそのまま返す
        return jsonify(parsed_data)

    except Exception as e:
        return jsonify({'error': f'OCR処理中にエラーが発生しました: {str(e)}'}), 500
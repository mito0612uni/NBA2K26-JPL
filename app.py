import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, case, or_
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from functools import wraps
from collections import defaultdict
from werkzeug.utils import secure_filename

# --- 1. アプリケーションとデータベースの初期設定 ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_very_secret_key_change_it'
basedir = os.path.abspath(os.path.dirname(__file__))

# 2. basedirを使ってアップロードフォルダを設定
UPLOAD_FOLDER = os.path.join(basedir, 'static/logos')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# 3. basedirを使ってデータベースの場所を設定
# Renderなどの本番環境では環境変数 DATABASE_URL を使い、なければローカルのsqliteを使う
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # HerokuなどのためのURL書き換え
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url.replace("postgres://", "postgresql://", 1)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ファイル形式をチェックするヘルパー関数
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- 2. ログインマネージャーの設定 ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "このページにアクセスするにはログインが必要です。"

# --- 3. データベースモデル（テーブル）の定義 ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    role = db.Column(db.String(20), nullable=False, default='user')
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    @property
    def is_admin(self): return self.role == 'admin'

class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    logo_image = db.Column(db.String(100), nullable=True)
    league = db.Column(db.String(50), nullable=True)
    players = db.relationship('Player', backref='team', lazy=True)

class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_date = db.Column(db.String(50))
    home_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    away_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    home_score = db.Column(db.Integer, default=0)
    away_score = db.Column(db.Integer, default=0)
    is_finished = db.Column(db.Boolean, default=False)
    youtube_url = db.Column(db.String(200), nullable=True)
    home_team = db.relationship('Team', foreign_keys=[home_team_id])
    away_team = db.relationship('Team', foreign_keys=[away_team_id])

class PlayerStat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    pts = db.Column(db.Integer, default=0); ast = db.Column(db.Integer, default=0)
    reb = db.Column(db.Integer, default=0); stl = db.Column(db.Integer, default=0)
    blk = db.Column(db.Integer, default=0); foul = db.Column(db.Integer, default=0)
    turnover = db.Column(db.Integer, default=0); fgm = db.Column(db.Integer, default=0)
    fga = db.Column(db.Integer, default=0); three_pm = db.Column(db.Integer, default=0)
    three_pa = db.Column(db.Integer, default=0); ftm = db.Column(db.Integer, default=0)
    fta = db.Column(db.Integer, default=0)
    player = db.relationship('Player')

# --- 4. 権限管理とヘルパー関数 ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("この操作には管理者権限が必要です。")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def calculate_standings(league_filter=None):
    if league_filter: teams = Team.query.filter_by(league=league_filter).all()
    else: teams = Team.query.all()
    standings = []
    for team in teams:
        wins, losses, points_for, points_against = 0, 0, 0, 0
        home_games = Game.query.filter_by(home_team_id=team.id, is_finished=True).all()
        for game in home_games:
            points_for += game.home_score; points_against += game.away_score
            if game.home_score > game.away_score: wins += 1
            elif game.home_score < game.away_score: losses += 1
        away_games = Game.query.filter_by(away_team_id=team.id, is_finished=True).all()
        for game in away_games:
            points_for += game.away_score; points_against += game.home_score
            if game.away_score > game.home_score: wins += 1
            elif game.away_score < game.home_score: losses += 1
        games_played = wins + losses
        points = (wins * 2) + (losses * 1)
        standings.append({
            'team': team, 'team_name': team.name, 'league': team.league, 'wins': wins, 'losses': losses, 'points': points,
            'avg_pf': round(points_for / games_played, 1) if games_played > 0 else 0,
            'avg_pa': round(points_against / games_played, 1) if games_played > 0 else 0,
            'diff': points_for - points_against,
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

# --- 5. ルート（ページの表示と処理） ---
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
    upcoming_games_list = Game.query.filter_by(is_finished=False).order_by(Game.game_date.asc()).all()
    games_by_date = defaultdict(list)
    for game in upcoming_games_list:
        games_by_date[game.game_date].append(game)
    return render_template('index.html', overall_standings=overall_standings,
                           league_a_standings=league_a_standings, league_b_standings=league_b_standings,
                           leaders=stats_leaders, games_by_date=games_by_date)

@app.route('/roster', methods=['GET', 'POST'])
@login_required
@admin_required
def roster():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_team':
            team_name = request.form.get('team_name')
            league = request.form.get('league')
            logo_filename = None
            if 'logo_image' in request.files:
                file = request.files['logo_image']
                if file and file.filename != '' and allowed_file(file.filename):
                    logo_filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], logo_filename))
                elif file.filename != '':
                    flash('許可されていないファイル形式です。'); return redirect(url_for('roster'))
            if team_name and league:
                if not Team.query.filter_by(name=team_name).first():
                    new_team = Team(name=team_name, league=league, logo_image=logo_filename)
                    db.session.add(new_team); db.session.commit()
                    flash(f'チーム「{team_name}」が{league}に登録されました。')
                else: flash(f'チーム「{team_name}」は既に存在します。')
            else: flash('チーム名とリーグを選択してください。')

        elif action == 'add_player':
            player_name = request.form.get('player_name')
            team_id = request.form.get('team_id')
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
                        user_to_promote.role = 'admin'
                        db.session.commit()
                        flash(f'ユーザー「{username_to_promote}」を管理者に昇格させました。')
                    else:
                        flash(f'ユーザー「{username_to_promote}」は既に管理者です。')
                else:
                    flash(f'ユーザー「{username_to_promote}」が見つかりません。')
            else:
                flash('ユーザー名を入力してください。')
        
        return redirect(url_for('roster'))

    teams = Team.query.all()
    users = User.query.all()
    return render_template('roster.html', teams=teams, users=users)
@app.route('/schedule')
def schedule():
    team_id = request.args.get('team_id', type=int)
    query = Game.query
    if team_id:
        query = query.filter(or_(Game.home_team_id == team_id, Game.away_team_id == team_id))
    games = query.order_by(Game.game_date.asc()).all()
    all_teams = Team.query.order_by(Team.name).all()
    return render_template('schedule.html', games=games, all_teams=all_teams, selected_team_id=team_id)

@app.route('/add_schedule', methods=['GET', 'POST'])
@login_required
@admin_required
def add_schedule():
    if request.method == 'POST':
        home_team_id = request.form['home_team_id']; away_team_id = request.form['away_team_id']
        if home_team_id == away_team_id:
            flash("ホームチームとアウェイチームは同じチームを選択できません。"); return redirect(url_for('add_schedule'))
        new_game = Game(game_date=request.form['game_date'], home_team_id=home_team_id, away_team_id=away_team_id)
        db.session.add(new_game); db.session.commit()
        flash("新しい試合日程が追加されました。"); return redirect(url_for('schedule'))
    teams = Team.query.all()
    return render_template('add_schedule.html', teams=teams)

@app.route('/team/delete/<int:team_id>', methods=['POST'])
@login_required
@admin_required
def delete_team(team_id):
    team_to_delete = Team.query.get_or_404(team_id)
    if team_to_delete.logo_image:
        logo_path = os.path.join(app.config['UPLOAD_FOLDER'], team_to_delete.logo_image)
        if os.path.exists(logo_path): os.remove(logo_path)
    Player.query.filter_by(team_id=team_id).delete()
    db.session.delete(team_to_delete); db.session.commit()
    flash(f'チーム「{team_to_delete.name}」と所属選手を削除しました。'); return redirect(url_for('roster'))

@app.route('/player/delete/<int:player_id>', methods=['POST'])
@login_required
@admin_required
def delete_player(player_id):
    player_to_delete = Player.query.get_or_404(player_id)
    player_name = player_to_delete.name
    db.session.delete(player_to_delete); db.session.commit()
    flash(f'選手「{player_name}」を削除しました。'); return redirect(url_for('roster'))

@app.route('/game/<int:game_id>/edit', methods=['GET', 'POST'])
def edit_game(game_id):
    game = Game.query.get_or_404(game_id)
    
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('結果を保存するにはログインが必要です。')
            return redirect(url_for('login'))
        
        # ★★★ YouTubeのURLを保存する処理を追加 ★★★
        game.youtube_url = request.form.get('youtube_url')
        
        home_total_score, away_total_score = 0, 0
        # ... (これ以降のスタッツ保存処理は変更なし) ...
        for team in [game.home_team, game.away_team]:
            for player in team.players:
                # フォームにデータがある選手のみ処理
                if f'player_{player.id}_pts' in request.form:
                    # ... (中身は省略) ...
                    if team.id == game.home_team_id: home_total_score += stat.pts
                    else: away_total_score += stat.pts
        
        game.home_score = home_total_score
        game.away_score = away_total_score
        game.is_finished = True
        
        db.session.commit()
        flash('試合結果が更新されました。')
        return redirect(url_for('schedule'))
        
    # GETリクエスト (ページ表示)
    stats = {str(stat.player_id): {
        # ... (中身は省略) ...
    } for stat in PlayerStat.query.filter_by(game_id=game_id).all()}
    
    return render_template('game_edit.html', game=game, stats=stats)@app.route('/stats')
def stats_page():
    games_played = func.count(PlayerStat.game_id).label('games_played')
    avg_pts = func.avg(PlayerStat.pts).label('avg_pts'); avg_ast = func.avg(PlayerStat.ast).label('avg_ast')
    avg_reb = func.avg(PlayerStat.reb).label('avg_reb'); avg_stl = func.avg(PlayerStat.stl).label('avg_stl')
    avg_blk = func.avg(PlayerStat.blk).label('avg_blk'); avg_foul = func.avg(PlayerStat.foul).label('avg_foul')
    avg_turnover = func.avg(PlayerStat.turnover).label('avg_turnover')
    avg_fgm = func.avg(PlayerStat.fgm).label('avg_fgm'); avg_fga = func.avg(PlayerStat.fga).label('avg_fga')
    avg_three_pm = func.avg(PlayerStat.three_pm).label('avg_three_pm'); avg_three_pa = func.avg(PlayerStat.three_pa).label('avg_three_pa')
    avg_ftm = func.avg(PlayerStat.ftm).label('avg_ftm'); avg_fta = func.avg(PlayerStat.fta).label('avg_fta')
    total_fgm = func.sum(PlayerStat.fgm); total_fga = func.sum(PlayerStat.fga)
    fg_percentage = case((total_fga > 0, (total_fgm * 100.0 / total_fga)), else_=0).label('fg_pct')
    total_3pm = func.sum(PlayerStat.three_pm); total_3pa = func.sum(PlayerStat.three_pa)
    three_p_percentage = case((total_3pa > 0, (total_3pm * 100.0 / total_3pa)), else_=0).label('three_p_pct')
    total_ftm = func.sum(PlayerStat.ftm); total_fta = func.sum(PlayerStat.fta)
    ft_percentage = case((total_fta > 0, (total_ftm * 100.0 / total_fta)), else_=0).label('ft_pct')
    all_stats = db.session.query(
        Player.name.label('player_name'), Team.name.label('team_name'), games_played,
        avg_pts, avg_ast, avg_reb, avg_stl, avg_blk, avg_foul, avg_turnover,
        avg_fgm, avg_fga, avg_three_pm, avg_three_pa, avg_ftm, avg_fta,
        fg_percentage, three_p_percentage, ft_percentage
    ).join(Player, PlayerStat.player_id == Player.id).join(Team, Player.team_id == Team.id).group_by(Player.id).all()
    return render_template('stats.html', all_stats=all_stats)

# --- 6. データベース初期化コマンドと実行 ---
@app.cli.command('init-db')
def init_db_command():
    db.drop_all()
    db.create_all()
    print('Initialized the database.')

if __name__ == '__main__':
    app.run(debug=False) # TrueからFalseに変更
import json, random, string, os, uuid
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, template_folder='templates')
app.secret_key = "secret"
app.wsgi_app = ProxyFix(app.wsgi_app)
socketio = SocketIO(app, async_mode="threading")

# Database configuration
DATABASE = "game_data.db"

# Separate valid slots (identifiers) from passwords
valid_slots = ["1", "2", "3"]  # These are the slot identifiers
passwords = ["11111", "22222", "33333"]  # These are the actual passwords for login

current_game_code = None
game_state = {}  # { code: { "1": {"sid":..., "name":...} или None } }
socket_registry = {}

def init_db():
    """Initialize the SQLite database with required tables."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Drop the historical_games table if it exists (as requested)
    cursor.execute("DROP TABLE IF EXISTS historical_games")
    
    # Create table for game sessions and player data
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT UNIQUE,
            current_game_code TEXT,
            start_time TEXT,
            end_time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create table for player data
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT,
            slot_id TEXT,
            name TEXT,
            token TEXT,
            connected BOOLEAN DEFAULT 0,
            red_button_state BOOLEAN DEFAULT 0,
            FOREIGN KEY (game_code) REFERENCES game_sessions (game_code),
            UNIQUE (game_code, slot_id)
        )
    ''')
    
    # Create table for scores
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_code TEXT,
            slot_id TEXT,
            round_number INTEGER,  -- 0-4 for 5 rounds (including shootout)
            round_name TEXT,       -- Name of the round ("Раунд I", "Раунд II", etc.)
            round_score INTEGER DEFAULT 0,
            total_score INTEGER DEFAULT 0,
            final_bet INTEGER DEFAULT NULL,  -- Bet amount in the final round
            final_bet_result INTEGER DEFAULT NULL,  -- Result of the final bet
            FOREIGN KEY (game_code) REFERENCES game_sessions (game_code),
            UNIQUE (game_code, slot_id, round_number)
        )
    ''')
    
    conn.commit()
    conn.close()

def save_playerdata(data):
    """Save player data to SQLite database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Save current game code
    current_game_code = data.get("current_game_code")
    if current_game_code:
        update_game_session(game_code=current_game_code, current_game_code=current_game_code)
    
    # Save session data
    sessions = data.get("sessions", {})
    if current_game_code in sessions:
        for slot_id, player_info in sessions[current_game_code].items():
            if player_info:
                update_player_session(
                    game_code=current_game_code,
                    slot_id=slot_id,
                    name=player_info.get("name"),
                    token=player_info.get("token"),
                    connected=player_info.get("connected", False)
                )
    
    # Save scores
    scores = data.get("scores", {})
    for slot_id, score_data in scores.items():
        if "rounds" in score_data and "total" in score_data:
            for round_num, round_score in enumerate(score_data["rounds"]):
                # Define round names
                round_names = ["Раунд I", "Раунд II", "Раунд III", "Финальный раунд", "Перестрелка"]
                round_name = round_names[round_num] if round_num < len(round_names) else f"Раунд {round_num + 1}"
                
                update_score(
                    game_code=current_game_code,
                    slot_id=slot_id,
                    round_number=round_num,
                    round_score=round_score,
                    total_score=score_data["total"],
                    round_name=round_name
                )
    
    conn.close()

# Initialize database at startup
init_db()

# ===== SQLite Database Functions =====
def load_playerdata():
    """Load player data from SQLite database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Get current game session
    cursor.execute("SELECT game_code, current_game_code, start_time, end_time FROM game_sessions ORDER BY id DESC LIMIT 1")
    session_row = cursor.fetchone()
    
    result = {
        "current_game_code": None,
        "sessions": {},
        "start_time": None,
        "end_time": None,
        "scores": {}
    }
    
    if session_row:
        game_code, current_game_code, start_time, end_time = session_row
        result["current_game_code"] = current_game_code
        result["start_time"] = start_time
        result["end_time"] = end_time
        
        # Load player sessions
        cursor.execute("SELECT slot_id, name, token, connected, red_button_state FROM players WHERE game_code = ?", (game_code,))
        players = cursor.fetchall()
        
        sessions = {}
        if game_code:
            # Initialize all slots for this game
            sessions[game_code] = {slot: None for slot in valid_slots}
            
            for slot_id, name, token, connected, red_button_state in players:
                sessions[game_code][slot_id] = {
                    "name": name,
                    "token": token,
                    "connected": bool(connected),
                    "red_button_state": bool(red_button_state)
                }
        
        result["sessions"] = sessions
        
        # Load scores
        cursor.execute("SELECT slot_id, round_number, round_score, total_score FROM scores WHERE game_code = ?", (game_code,))
        scores_data = cursor.fetchall()
        
        # Initialize scores structure
        scores = {}
        for slot in valid_slots:
            scores[slot] = {
                "rounds": [0, 0, 0, 0, 0],  # 5 rounds (including shootout)
                "total": 0
            }
        
        # Populate scores from database
        for slot_id, round_num, round_score, total_score in scores_data:
            if slot_id in scores:
                if 0 <= round_num < 5:  # Now supporting 5 rounds
                    scores[slot_id]["rounds"][round_num] = round_score
                scores[slot_id]["total"] = total_score
        
        result["scores"] = scores
    
    conn.close()
    return result

def update_game_session(game_code, current_game_code=None, start_time=None, end_time=None):
    """Update or create a game session in the database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Check if game session already exists
    cursor.execute("SELECT id FROM game_sessions WHERE game_code = ?", (game_code,))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing session
        cursor.execute("""
            UPDATE game_sessions 
            SET current_game_code = COALESCE(?, current_game_code),
                start_time = COALESCE(?, start_time),
                end_time = COALESCE(?, end_time)
            WHERE game_code = ?
        """, (current_game_code, start_time, end_time, game_code))
    else:
        # Create new session
        cursor.execute("""
            INSERT INTO game_sessions (game_code, current_game_code, start_time, end_time)
            VALUES (?, ?, ?, ?)
        """, (game_code, current_game_code, start_time, end_time))
    
    conn.commit()
    conn.close()

def update_red_button_state(game_code, slot_id, red_button_state):
    """Update the red button state for a specific player in the database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Update the red button state for the player
    cursor.execute("""
        UPDATE players 
        SET red_button_state = ?
        WHERE game_code = ? AND slot_id = ?
    """, (red_button_state, game_code, slot_id))
    
    conn.commit()
    conn.close()


def update_player_session(game_code, slot_id, name=None, token=None, connected=None, red_button_state=None):
    """Update or create a player session in the database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Check if player session already exists
    cursor.execute("SELECT id FROM players WHERE game_code = ? AND slot_id = ?", (game_code, slot_id))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing player
        update_fields = []
        params = []
        
        if name is not None:
            update_fields.append("name = ?")
            params.append(name)
        if token is not None:
            update_fields.append("token = ?")
            params.append(token)
        if connected is not None:
            update_fields.append("connected = ?")
            params.append(connected)
        if red_button_state is not None:
            update_fields.append("red_button_state = ?")
            params.append(red_button_state)
        
        if update_fields:
            sql = f"UPDATE players SET {', '.join(update_fields)} WHERE game_code = ? AND slot_id = ?"
            params.extend([game_code, slot_id])
            cursor.execute(sql, params)
    else:
        # Create new player
        cursor.execute("""
            INSERT INTO players (game_code, slot_id, name, token, connected, red_button_state)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (game_code, slot_id, name, token, connected or False, red_button_state or False))
    
    conn.commit()
    conn.close()

def update_score(game_code, slot_id, round_number, round_score=None, total_score=None, round_name=None, final_bet=None, final_bet_result=None):
    """Update or create a score record in the database."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Check if score record already exists
    cursor.execute("SELECT id FROM scores WHERE game_code = ? AND slot_id = ? AND round_number = ?", 
                   (game_code, slot_id, round_number))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing score
        cursor.execute("""
            UPDATE scores 
            SET round_score = COALESCE(?, round_score),
                total_score = COALESCE(?, total_score),
                round_name = COALESCE(?, round_name),
                final_bet = COALESCE(?, final_bet),
                final_bet_result = COALESCE(?, final_bet_result)
            WHERE game_code = ? AND slot_id = ? AND round_number = ?
        """, (round_score, total_score, round_name, final_bet, final_bet_result, game_code, slot_id, round_number))
    else:
        # Create new score record
        cursor.execute("""
            INSERT INTO scores (game_code, slot_id, round_number, round_name, round_score, total_score, final_bet, final_bet_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (game_code, slot_id, round_number, round_name, round_score or 0, total_score or 0, final_bet, final_bet_result))
    
    conn.commit()
    conn.close()


# ===== Утилиты =====
def generate_code():
    letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    digits = ''.join(random.choices(string.digits, k=5))
    return f"{letters}-{digits}"

def ensure_code_state(code):
    if code not in game_state:
        game_state[code] = {s: None for s in valid_slots}

# ===== HTTP маршруты =====
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_val = request.form.get("login") or ""
        password = request.form.get("password") or ""
        role = request.form.get("role") or ""
        access_code = request.form.get("access_code") or ""

        if role == "Администратор":
            if login_val == "Admin" and password == "Administrator":
                session.clear()
                session["role"] = "admin"
                return redirect(url_for("admin"))
            flash("Неверные данные администратора")
            return redirect(url_for("login"))

        if role == "Игрок":
            if current_game_code is None or access_code != current_game_code:
                flash("Неверный код доступа или сеанс не активен")
                return redirect(url_for("login"))
            
            # Map password to slot ID
            if password not in passwords:
                flash("Неверный пароль игрока")
                return redirect(url_for("login"))
            
            # Find the corresponding slot ID
            password_index = passwords.index(password)
            slot_id = valid_slots[password_index]

            ensure_code_state(current_game_code)
            pdata = load_playerdata()

            # Инициализация sessions[current_game_code], если отсутствует
            if current_game_code not in pdata["sessions"]:
                pdata["sessions"][current_game_code] = {s: None for s in valid_slots}
                save_playerdata(pdata)

            slot_info = pdata["sessions"][current_game_code][slot_id]

            # Проверка: занят ли слот другим игроком
            if slot_info and slot_info.get("connected"):
                session_token = session.get("player_token")
                if session_token and slot_info.get("token") != session_token:
                    flash("Этот слот уже занят другим игроком")
                    return redirect(url_for("login"))
                elif not session_token and slot_info.get("name") != login_val:
                    flash("Этот слот уже занят другим игроком")
                    return redirect(url_for("login"))

            # Генерация/восстановление токена
            if slot_info and slot_info.get("token"):
                player_token = slot_info["token"]
            else:
                player_token = str(uuid.uuid4())
                pdata["sessions"][current_game_code][slot_id] = {
                    "name": login_val,
                    "token": player_token,
                    "connected": False
                }
                save_playerdata(pdata)

            session.clear()
            session["role"] = "player"
            session["player_id"] = slot_id
            session["player_name"] = login_val
            session["code"] = current_game_code
            session["player_token"] = player_token
            return redirect(url_for("player", player_id=slot_id))

    return render_template("login.html")

@app.route("/player/<player_id>")
def player(player_id):
    if session.get("role") != "player":
        return redirect(url_for("login"))
    player_token = session.get("player_token")
    if not player_token:
        return redirect(url_for("login"))
    return render_template("player.html",
                           player_id=player_id,
                           game_code=session.get("code"),
                           player_name=session.get("player_name"),
                           player_token=player_token)

@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    return render_template("admin.html", game_code=current_game_code)

@app.route("/generate_code", methods=["POST"])
def generate_code_route():
    global current_game_code
    code = generate_code()
    current_game_code = code
    ensure_code_state(code)
    game_state[code] = {s: None for s in valid_slots}

    # Initialize game session in database
    update_game_session(game_code=code, current_game_code=code)

    # Initialize player sessions in database
    for slot in valid_slots:
        update_player_session(game_code=code, slot_id=slot, name=None, token=None, connected=False)

    # Initialize scores in database
    for slot in valid_slots:
        for round_num in range(5):  # 5 rounds (including shootout)
            round_names = ["Раунд I", "Раунд II", "Раунд III", "Финальный раунд", "Перестрелка"]
            round_name = round_names[round_num] if round_num < len(round_names) else f"Раунд {round_num + 1}"
            update_score(game_code=code, slot_id=slot, round_number=round_num, round_score=0, total_score=0, round_name=round_name)

    socketio.emit("code_updated", {"code": current_game_code})
    return redirect(url_for("admin"))


@app.route("/start_game", methods=["POST"])
def start_game():
    global current_game_code
    if current_game_code:
        from datetime import datetime
        start_time = datetime.now().isoformat()
        
        # Update game session with start time
        update_game_session(game_code=current_game_code, start_time=start_time)
        
        # Reset scores for all slots and rounds
        for slot in valid_slots:
            for round_num in range(5):  # 5 rounds (including shootout)
                round_names = ["Раунд I", "Раунд II", "Раунд III", "Финальный раунд", "Перестрелка"]
                round_name = round_names[round_num] if round_num < len(round_names) else f"Раунд {round_num + 1}"
                update_score(game_code=current_game_code, slot_id=slot, 
                           round_number=round_num, round_score=0, total_score=0, round_name=round_name)
    return redirect(url_for("admin"))

@app.route("/restore_code", methods=["POST"])
def restore_code_route():
    global current_game_code
    pdata = load_playerdata()
    code = pdata.get("current_game_code")
    if code:
        current_game_code = code
        ensure_code_state(code)
        # Initialize if missing
        socketio.emit("code_updated", {"code": current_game_code})
    return redirect(url_for("admin"))

@app.route("/end_session", methods=["POST"])
def end_session():
    global current_game_code
    if current_game_code:
        room = current_game_code
        socketio.emit("session_ended", room=room)
        game_state[current_game_code] = {s: None for s in valid_slots}

        # Save game data to history before clearing
        save_game_to_history(current_game_code)
        
        # Update game session with end time
        from datetime import datetime
        end_time = datetime.now().isoformat()
        update_game_session(game_code=current_game_code, end_time=end_time)
        
        # Disconnect all players
        for slot in valid_slots:
            update_player_session(game_code=current_game_code, slot_id=slot, connected=False)

        current_game_code = None
        socketio.emit("code_updated", {"code": None})
    return redirect(url_for("login"))

@app.route("/room_snapshot")
def room_snapshot():
    code = request.args.get("code")
    if not code:
        return {"slots": {}}

    # Load player sessions from database
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT slot_id, name, connected FROM players WHERE game_code = ?", (code,))
    players = cursor.fetchall()
    
    conn.close()
    
    # Create snapshot dictionary
    snapshot = {s: None for s in valid_slots}
    
    for slot_id, name, connected in players:
        if slot_id in valid_slots and connected:
            snapshot[slot_id] = name

    return {"slots": snapshot}

@app.route("/logout_player", methods=["POST"])
def logout_player():
    player_id = session.get("player_id")
    code = session.get("code")
    if code and player_id:
        # Update player session in database
        update_player_session(game_code=code, slot_id=player_id, connected=False)
        
        socketio.emit("player_update", {
            "player_id": player_id,
            "status": False,
            "name": None
        }, room=code)
    session.clear()
    return redirect(url_for("login"))

# ===== Socket.IO =====
@socketio.on("connect")
def on_connect():
    socket_registry[request.sid] = {"role": None, "code": None, "slot": None}


@app.route("/update_score", methods=["POST"])
def update_score_http():
    slot = request.form.get("slot")
    points = request.form.get("points", type=int)
    operation = request.form.get("operation")  # "add" or "subtract"
    
    if slot and points is not None and operation in ["add", "subtract"]:
        pdata = load_playerdata()
        
        if slot in pdata["scores"]:
            if operation == "add":
                pdata["scores"][slot]["total"] += points
            elif operation == "subtract":
                # Allow negative scores - don't use max(0, ...)
                pdata["scores"][slot]["total"] -= points
            
            save_playerdata(pdata)
    
    return redirect(url_for("admin"))


@app.route("/get_player_scores")
def get_player_scores():
    pdata = load_playerdata()
    return {"scores": pdata["scores"], "start_time": pdata.get("start_time"), "end_time": pdata.get("end_time")}


@socketio.on("update_player_score")
def handle_update_player_score(data):
    slot = data.get("slot")
    points = data.get("points", 0)
    operation = data.get("operation")  # "add" or "subtract"
    round_number = data.get("round", 0)  # 0-based index for rounds (0-4 now including shootout)
    
    if slot and points is not None and operation in ["add", "subtract"]:
        pdata = load_playerdata()
        
        if slot in pdata["scores"]:
            if operation == "add":
                # Update round score if round is specified
                if 0 <= round_number < 5:  # Now supporting 5 rounds
                    pdata["scores"][slot]["rounds"][round_number] += points
                # Update total score
                pdata["scores"][slot]["total"] += points
            elif operation == "subtract":
                # Update round score if round is specified
                if 0 <= round_number < 5:  # Now supporting 5 rounds
                    # Allow negative scores
                    pdata["scores"][slot]["rounds"][round_number] -= points
                # Update total score
                # Allow negative scores - don't use max(0, ...)
                pdata["scores"][slot]["total"] -= points
            
            save_playerdata(pdata)
            
            # Отправляем обновленные данные всем участникам комнаты
            code = data.get("code", current_game_code)
            emit("score_updated", {
                "slot": slot,
                "total": pdata["scores"][slot]["total"],
                "rounds": pdata["scores"][slot]["rounds"]
            }, room=code)

@socketio.on("disconnect")
def on_disconnect():
    info = socket_registry.pop(request.sid, None)
    if not info:
        return
    role, code, slot = info["role"], info["code"], info["slot"]
    if role == "player" and code and slot:
        pdata = load_playerdata()
        sessions = pdata.get("sessions", {})
        if isinstance(sessions, dict) and code in sessions:
            current_slot = sessions[code].get(slot)
            if isinstance(current_slot, dict):
                # Проверяем другие соединения для этого слота
                other_connections_exist = any(
                    v.get("role") == "player" and v.get("code") == code and v.get("slot") == slot
                    for v in socket_registry.values()
                )
                if not other_connections_exist:
                    current_slot["connected"] = False
                    save_playerdata(pdata)

        ensure_code_state(code)
        if game_state.get(code, {}).get(slot) and game_state[code][slot]["sid"] == request.sid:
            # Проверка других подключений в game_state (дубликатов не бывает — только socket_registry)
            other_connections_exist = any(
                v.get("role") == "player" and v.get("code") == code and v.get("slot") == slot
                for v in socket_registry.values()
            )
            if not other_connections_exist:
                game_state[code][slot] = None
                emit("player_update", {"player_id": slot, "status": False, "name": None}, room=code)
        leave_room(code)

@socketio.on("admin_join")
def admin_join(data):
    code = data.get("code")
    socket_registry[request.sid] = {"role": "admin", "code": code, "slot": None}
    if code:
        ensure_code_state(code)
        join_room(code)
        pdata = load_playerdata()
        sessions = pdata.get("sessions", {})
        if isinstance(sessions, dict) and code in sessions:
            current_sessions = sessions[code]
            if not isinstance(current_sessions, dict):
                current_sessions = {s: None for s in valid_slots}
            snapshot = {}
            for s in valid_slots:
                info = current_sessions.get(s)
                snapshot[s] = info["name"] if info and info.get("connected") else None
        else:
            snapshot = {s: None for s in valid_slots}
        # Подготовим информацию о желтых индикаторах
        yellow_indicators = {}
        if active_signal["active"] and active_signal["code"] == code:
            yellow_indicators[active_signal["player_id"]] = True
        
        emit("admin_state", {
            "code": code, 
            "slots": snapshot,
            "yellowIndicators": yellow_indicators
        })

@socketio.on("join_player")
def handle_join_player(data):
    player_id = data.get("player_id")
    code = data.get("code")
    player_name = data.get("name")
    token = data.get("token")

    if code is None or (current_game_code and code != current_game_code):
        emit("join_error", {"message": "Сеанс недоступен или устарел"})
        return
    if player_id not in valid_slots:
        emit("join_error", {"message": "Недействительный слот"})
        return

    ensure_code_state(code)
    pdata = load_playerdata()
    sessions = pdata.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        pdata["sessions"] = sessions

    if code not in sessions:
        sessions[code] = {s: None for s in valid_slots}

    slot_info = sessions[code][player_id]

    # Проверки на конфликт
    if slot_info and slot_info.get("connected"):
        if token and slot_info.get("token") != token:
            emit("join_error", {"message": "Слот уже занят другим игроком"})
            return
        elif not token and slot_info.get("name") != player_name:
            emit("join_error", {"message": "Слот уже занят другим игроком"})
            return

    # Разрешить вход при: пустой слот, offline, или совпадающий токен/имя
    if (slot_info is None or
        not slot_info.get("connected") or
        (token and slot_info.get("token") == token) or
        (not token and slot_info.get("name") == player_name)):

        if slot_info is None:
            sessions[code][player_id] = {
                "name": player_name,
                "token": token or str(uuid.uuid4()),
                "connected": True
            }
        else:
            # Обновляем имя и connected, сохраняя оригинальный токен
            sessions[code][player_id]["name"] = player_name
            sessions[code][player_id]["connected"] = True

        save_playerdata(pdata)

        # Обновление game_state: замена старого SID
        if game_state.get(code, {}).get(player_id):
            old_sid = game_state[code][player_id]["sid"]
            if old_sid != request.sid:
                # Можно добавить leave_room(old_sid), но Socket.IO сам отслеживает disconnect
                pass

        game_state[code][player_id] = {"sid": request.sid, "name": player_name}
        socket_registry[request.sid] = {"role": "player", "code": code, "slot": player_id}
        join_room(code)

        emit("player_update", {"player_id": player_id, "status": True, "name": player_name}, room=code)
        # Обновляем админов
        snapshot = {s: (pdata["sessions"][code][s]["name"]
                        if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                        else None) for s in valid_slots}
        # Подготовим информацию о желтых индикаторах
        yellow_indicators = {}
        if active_signal["active"] and active_signal["code"] == code:
            yellow_indicators[active_signal["player_id"]] = True
        
        emit("admin_state", {
            "code": code, 
            "slots": snapshot,
            "yellowIndicators": yellow_indicators
        }, room=code)
    else:
        emit("join_error", {"message": "Слот недоступен"})

@socketio.on("request_admin_snapshot")
def request_admin_snapshot(data):
    code = data.get("code")
    if not code:
        # Подготовим информацию о желтых индикаторах
        yellow_indicators = {}
        if active_signal["active"] and active_signal["code"] is None:
            yellow_indicators[active_signal["player_id"]] = True
        
        emit("admin_state", {
            "code": None, 
            "slots": {s: None for s in valid_slots},
            "yellowIndicators": yellow_indicators
        })
        return

    pdata = load_playerdata()
    if code not in pdata["sessions"]:
        # Подготовим информацию о желтых индикаторах
        yellow_indicators = {}
        if active_signal["active"] and active_signal["code"] == code:
            yellow_indicators[active_signal["player_id"]] = True
        
        emit("admin_state", {
            "code": code, 
            "slots": {s: None for s in valid_slots},
            "yellowIndicators": yellow_indicators
        })
        return
    snapshot = {s: (pdata["sessions"][code][s]["name"]
                    if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                    else None) for s in valid_slots}
    # Подготовим информацию о желтых индикаторах
    yellow_indicators = {}
    if active_signal["active"] and active_signal["code"] == code:
        yellow_indicators[active_signal["player_id"]] = True
    
    emit("admin_state", {
        "code": code, 
        "slots": snapshot,
        "yellowIndicators": yellow_indicators
    })


# ===== Новые обработчики для сигнала игроков =====

# Глобальное состояние для отслеживания активного сигнала
active_signal = {
    "code": None,           # Код игры, в которой активирован сигнал
    "player_id": None,      # ID игрока, который нажал кнопку
    "active": False         # Активен ли сигнал в данный момент
}

@socketio.on("player_signal")
def handle_player_signal(data):
    global active_signal
    
    player_id = data.get("player_id")
    code = data.get("code")
    player_name = data.get("name")
    token = data.get("token")
    
    # Проверяем, что игра активна и совпадает код
    if code != current_game_code:
        emit("join_error", {"message": "Сеанс недоступен или устарел"})
        return
    
    # Проверяем, что сигнал ещё не активирован другим игроком
    if active_signal["active"] and active_signal["code"] == code:
        # Уведомляем игрока, что сигнал уже активирован
        emit("signal_triggered", {
            "blockedPlayerId": player_id,
            "winnerPlayerId": active_signal["player_id"],
            "yellowIndicators": {active_signal["player_id"]: True}
        })
        return
    
    # Проверяем токен игрока
    pdata = load_playerdata()
    if (code in pdata["sessions"] and 
        player_id in pdata["sessions"][code] and
        pdata["sessions"][code][player_id] and
        pdata["sessions"][code][player_id].get("token") != token):
        emit("join_error", {"message": "Неверный токен игрока"})
        return
    
    # Активируем сигнал
    active_signal["code"] = code
    active_signal["player_id"] = player_id
    active_signal["active"] = True
    
    # Отправляем сигнал всем участникам комнаты
    emit("player_signal_received", {
        "player_id": player_id,
        "name": player_name
    }, room=code)
    
    # Отправляем обновление состояния с активированным желтым индикатором
    yellow_indicators = {player_id: True}
    emit("signal_triggered", {
        "blockedPlayerId": player_id,
        "winnerPlayerId": player_id,  # Добавляем идентификатор победителя
        "yellowIndicators": yellow_indicators
    }, room=code)
    
    # Автоматическая разблокировка через 10 секунд
    from threading import Timer
    timer = Timer(10.0, auto_unlock_signal, args=[code])
    timer.start()

def auto_unlock_signal(code):
    global active_signal
    # Проверяем, что сигнал всё ещё активен и относится к той же игре
    if active_signal["active"] and active_signal["code"] == code:
        # Get the player who activated the signal to reset their red button state
        player_id = active_signal["player_id"]
        
        # Сбрасываем активный сигнал
        active_signal["code"] = None
        active_signal["player_id"] = None
        active_signal["active"] = False
        
        # Reset the red button state for the player in database
        if player_id:
            update_red_button_state(game_code=code, slot_id=player_id, red_button_state=False)

        # Отправляем сигнал разблокировки всем участникам комнаты
        emit("signal_unlocked", {
            "players": valid_slots  # Разблокируем кнопки для всех игроков
        }, room=code)

@socketio.on("admin_unlock_signal")
def handle_admin_unlock_signal(data):
    global active_signal
    
    code = data.get("code")
    slot = data.get("slot")
    
    # Проверяем, что сигнал действительно был активирован
    if active_signal["active"] and active_signal["code"] == code:
        # Сбрасываем активный сигнал
        active_signal["code"] = None
        active_signal["player_id"] = None
        active_signal["active"] = False
        
        # Отправляем сигнал разблокировки всем участникам комнаты
        emit("signal_unlocked", {
            "players": valid_slots  # Разблокируем кнопки для всех игроков
        }, room=code)


@socketio.on("round_selected")
def handle_round_selected(data):
    """Handle round selection event from admin panel"""
    code = data.get("code")
    round_number = data.get("round_number")
    round_name = data.get("round_name")
    
    # We can store this information or just acknowledge the selection
    # For now, we'll just emit an event back to confirm the round selection
    emit("round_selection_confirmed", {
        "round_number": round_number,
        "round_name": round_name
    }, room=code)


# ===== Запуск =====
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=21365)
import json, random, string, os, uuid  # ← добавлен uuid
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = "secret"
app.wsgi_app = ProxyFix(app.wsgi_app)
socketio = SocketIO(app, async_mode="threading")

playerdata_file = "playerdata.json"
current_game_code = None
game_state = {}   # { code: { "11111": {"sid":..., "name":...} или None } }
socket_registry = {}
valid_slots = ["11111","22222","33333"]

# ===== Работа с файлом playerdata.json =====
def load_playerdata():
    try:
        with open(playerdata_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"current_game_code": None, "sessions": {}}

def save_playerdata(data):
    with open(playerdata_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
            if password not in valid_slots:
                flash("Неверный пароль игрока")
                return redirect(url_for("login"))
            ensure_code_state(current_game_code)
            # Загружаем playerdata ДО проверки слота
            pdata = load_playerdata()
            if current_game_code not in pdata["sessions"]:
                pdata["sessions"][current_game_code] = {s: None for s in valid_slots}
            # Проверяем, есть ли уже запись для этого пароля
            slot_info = pdata["sessions"][current_game_code][password]
            # Если слот занят активным игроком — запрещаем
            if slot_info and slot_info.get("connected"):
                flash("Этот слот уже занят")
                return redirect(url_for("login"))
            # Генерируем или восстанавливаем токен
            if slot_info and slot_info.get("token"):
                player_token = slot_info["token"]  # восстанавливаем
            else:
                player_token = str(uuid.uuid4())  # новый
                pdata["sessions"][current_game_code][password] = {
                    "name": login_val,
                    "token": player_token,
                    "connected": False
                }
                save_playerdata(pdata)
            session.clear()
            session["role"] = "player"
            session["player_id"] = password
            session["player_name"] = login_val
            session["code"] = current_game_code
            session["player_token"] = player_token  # ← сохраняем в session
            return redirect(url_for("player", player_id=password))
    return render_template("login.html")

@app.route("/player/<player_id>")
def player(player_id):
    if session.get("role") != "player":
        return redirect(url_for("login"))
    return render_template("player.html",
                           player_id=player_id,
                           game_code=session.get("code"),
                           player_name=session.get("player_name"),
                           player_token=session.get("player_token"))  # ← передаём токен

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
    pdata = load_playerdata()
    pdata["current_game_code"] = code
    pdata["sessions"][code] = {s: None for s in valid_slots}
    save_playerdata(pdata)
    socketio.emit("code_updated", {"code": current_game_code})
    return redirect(url_for("admin"))

@app.route("/restore_code", methods=["POST"])
def restore_code_route():
    global current_game_code
    code = None
    try:
        with open("gamecodes.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            code = data[-1] if data else None
    except Exception as e:
        print("Ошибка чтения gamecodes.json:", e)
    if code:
        current_game_code = code
        ensure_code_state(code)
        pdata = load_playerdata()
        pdata["current_game_code"] = code
        if code not in pdata["sessions"]:
            pdata["sessions"][code] = {s: None for s in valid_slots}
        save_playerdata(pdata)
        socketio.emit("code_updated", {"code": current_game_code})
    return redirect(url_for("admin"))

@app.route("/end_session", methods=["POST"])
def end_session():
    global current_game_code
    if current_game_code:
        room = current_game_code
        socketio.emit("session_ended", room=room)
        game_state[current_game_code] = {s: None for s in valid_slots}
        pdata = load_playerdata()
        pdata["sessions"][current_game_code] = {s: None for s in valid_slots}
        pdata["current_game_code"] = None
        save_playerdata(pdata)
        current_game_code = None
        socketio.emit("code_updated", {"code": None})
    return redirect(url_for("login"))

@app.route("/room_snapshot")
def room_snapshot():
    code = request.args.get("code")
    pdata = load_playerdata()
    if not code or code not in pdata["sessions"]:
        return {"slots": {}}
    snapshot = {s: (pdata["sessions"][code][s]["name"]
                    if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                    else None) for s in valid_slots}
    return {"slots": snapshot}

@app.route("/logout_player", methods=["POST"])
def logout_player():
    player_id = session.get("player_id")
    code = session.get("code")
    if code and player_id:
        pdata = load_playerdata()
        if code in pdata["sessions"] and pdata["sessions"][code].get(player_id):
            # Очищаем connected, но оставляем запись (для восстановления)
            pdata["sessions"][code][player_id]["connected"] = False
            save_playerdata(pdata)
            socketio.emit("player_update", {
                "player_id": player_id,
                "status": False,
                "name": None
            }, room=code)
        ensure_code_state(code)
        game_state[code][player_id] = None
    session.clear()
    return redirect(url_for("login"))

# ===== Socket.IO =====
from flask import request

@socketio.on("connect")
def on_connect():
    socket_registry[request.sid] = {"role": None, "code": None, "slot": None}

@socketio.on("disconnect")
def on_disconnect():
    info = socket_registry.pop(request.sid, None)
    if not info:
        return
    role, code, slot = info["role"], info["code"], info["slot"]
    if role == "player" and code and slot:
        pdata = load_playerdata()
        if code in pdata["sessions"] and pdata["sessions"][code].get(slot):
            # Только отключаем, не удаляем запись
            pdata["sessions"][code][slot]["connected"] = False
            save_playerdata(pdata)
        ensure_code_state(code)
        if game_state.get(code, {}).get(slot) and game_state[code][slot]["sid"] == request.sid:
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
        if code in pdata["sessions"]:
            snapshot = {s: (pdata["sessions"][code][s]["name"]
                            if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                            else None) for s in valid_slots}
        else:
            snapshot = {s: None for s in valid_slots}
        emit("admin_state", {"code": code, "slots": snapshot})

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
    if code not in pdata["sessions"]:
        pdata["sessions"][code] = {s: None for s in valid_slots}
    slot_info = pdata["sessions"][code][player_id]

    # Логика восстановления/занятия:
    if slot_info and slot_info.get("connected"):
        emit("join_error", {"message": "Слот уже занят"})
        return
    # Разрешаем вход, если:
    # - слот пуст, ИЛИ
    # - слот занят, но игрок offline (connected=False)
    # При этом: НЕ перезаписываем token при восстановлении!
    if slot_info is None or not slot_info.get("connected"):
        if slot_info is None:
            # Новый слот: создаём запись
            pdata["sessions"][code][player_id] = {
                "name": player_name,
                "token": token,
                "connected": True
            }
        else:
            # Восстановление: обновляем только connected и name (token остаётся прежним!)
            pdata["sessions"][code][player_id]["name"] = player_name
            pdata["sessions"][code][player_id]["connected"] = True
        save_playerdata(pdata)
        game_state[code][player_id] = {"sid": request.sid, "name": player_name}
        socket_registry[request.sid] = {"role": "player", "code": code, "slot": player_id}
        join_room(code)
        emit("player_update", {"player_id": player_id, "status": True, "name": player_name}, room=code)
        # Обновляем админов
        snapshot = {s: (pdata["sessions"][code][s]["name"]
                        if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                        else None) for s in valid_slots}
        emit("admin_state", {"code": code, "slots": snapshot}, room=code)
    else:
        # Должно быть недостижимо, но на всякий случай
        emit("join_error", {"message": "Слот недоступен"})

@socketio.on("request_admin_snapshot")
def request_admin_snapshot(data):
    code = data.get("code")
    if not code:
        emit("admin_state", {"code": None, "slots": {s: None for s in valid_slots}})
        return
    pdata = load_playerdata()
    if code not in pdata["sessions"]:
        emit("admin_state", {"code": code, "slots": {s: None for s in valid_slots}})
        return
    snapshot = {s: (pdata["sessions"][code][s]["name"]
                    if pdata["sessions"][code][s] and pdata["sessions"][code][s].get("connected")
                    else None) for s in valid_slots}
    emit("admin_state", {"code": code, "slots": snapshot})

# ===== Запуск =====
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=21365)
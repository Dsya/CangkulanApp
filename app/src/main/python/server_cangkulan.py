import http.server
import socketserver
import json
import random
import urllib.parse
import socket
import time
import sys          # Perbaikan 1: Mencegah NameError saat exit
import threading    # Perbaikan 2: Mencegah Race Condition
import os           # Untuk mencari path file aset statis (bgm4.mp3)
import mimetypes    # Untuk menebak Content-Type file statis (audio/mpeg, dll)

PORT = 8000
# Folder tempat script ini berada -> tempat mencari file aset statis seperti bgm4.mp3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# GAME STATE CONFIGURATION
# ==========================================
GAME_STATE = {
    "status": "waiting",  # waiting, playing, game_over
    "players": [],        # list: {id, name, cards[], finished, rank, last_seen, left, ready}
    "deck": [],
    "discard_pile": [],
    "current_round_cards": [], # list: {player_id, card}
    "current_turn_idx": 0,
    "leading_suit": None,
    "logs": [],
    "round_winner_id": None,
    "round_end_time": 0,  # timestamp ketika ronde transisi
    "rank_counter": 1,
    "host_id": None  # id pemain yang berperan sebagai host/pemilik lobby
}

def reassign_host_if_needed():
    """Jika host_id tidak lagi ada di antara pemain (keluar/DC), pindahkan status host
    ke pemain pertama yang tersisa di daftar."""
    global GAME_STATE
    host_still_present = any(p["id"] == GAME_STATE["host_id"] for p in GAME_STATE["players"])
    if not host_still_present:
        if GAME_STATE["players"]:
            new_host = GAME_STATE["players"][0]
            GAME_STATE["host_id"] = new_host["id"]
            GAME_STATE["logs"].append(f"👑 {new_host['name']} kini menjadi host lobby baru.")
        else:
            GAME_STATE["host_id"] = None

# Lock Threading Global untuk mengamankan modifikasi data GAME_STATE
GAME_STATE_LOCK = threading.Lock()

def get_card_value(card_str):
    _, val_str = card_str.split("-")
    val_map = {
        "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
        "J": 11, "Q": 12, "K": 13, "A": 14
    }
    return val_map.get(val_str, 0)

def player_has_suit(player, suit):
    for card in player["cards"]:
        if card.split("-")[0] == suit:
            return True
    return False

def draw_card_from_deck():
    global GAME_STATE
    if len(GAME_STATE["deck"]) == 0:
        if len(GAME_STATE["discard_pile"]) == 0:
            return None
        GAME_STATE["deck"] = list(GAME_STATE["discard_pile"])
        GAME_STATE["discard_pile"] = []
        random.shuffle(GAME_STATE["deck"])
        GAME_STATE["logs"].append("🔄 Tumpukan habis! Kartu buangan didaur ulang menjadi tumpukan cangkulan baru.")
    return GAME_STATE["deck"].pop()

def advance_turn():
    global GAME_STATE
    idx = GAME_STATE["current_turn_idx"]
    for i in range(1, len(GAME_STATE["players"]) + 1):
        next_idx = (idx + i) % len(GAME_STATE["players"])
        if not GAME_STATE["players"][next_idx]["finished"]:
            GAME_STATE["current_turn_idx"] = next_idx
            return

def is_round_complete():
    active_player_ids = {p["id"] for p in GAME_STATE["players"] if not p["finished"]}
    played_player_ids = {item["player_id"] for item in GAME_STATE["current_round_cards"]}
    return active_player_ids.issubset(played_player_ids)

def determine_round_winner():
    leading = GAME_STATE["leading_suit"]
    best_val = -1
    winner_id = None
    winner_card = None
    
    for item in GAME_STATE["current_round_cards"]:
        card = item["card"]
        suit, _ = card.split("-")
        if suit == leading:
            val = get_card_value(card)
            if val > best_val:
                best_val = val
                winner_id = item["player_id"]
                winner_card = card
                
    return winner_id, winner_card

def resolve_round_transition():
    global GAME_STATE
    
    for item in GAME_STATE["current_round_cards"]:
        GAME_STATE["discard_pile"].append(item["card"])
    GAME_STATE["current_round_cards"] = []
    
    winner_id = GAME_STATE["round_winner_id"]
    GAME_STATE["round_winner_id"] = None
    GAME_STATE["leading_suit"] = None
    
    for p in GAME_STATE["players"]:
        if not p["finished"] and len(p["cards"]) == 0:
            p["finished"] = True
            p["rank"] = GAME_STATE["rank_counter"]
            GAME_STATE["rank_counter"] += 1
            GAME_STATE["logs"].append(f"🎉 {p['name']} menang & habis kartunya! (Rank {p['rank']})")
            
    active_players = [p for p in GAME_STATE["players"] if not p["finished"]]
    if len(active_players) <= 1:
        GAME_STATE["status"] = "game_over"
        if len(active_players) == 1:
            loser = active_players[0]
            loser["finished"] = True
            loser["rank"] = GAME_STATE["rank_counter"]
            GAME_STATE["logs"].append(f"💀 GAME OVER! {loser['name']} adalah sang Pecangkul (Kalah)!")
        else:
            GAME_STATE["logs"].append("🏁 Game selesai!")
        return
        
    winner_player = next(p for p in GAME_STATE["players"] if p["id"] == winner_id)
    if not winner_player["finished"]:
        GAME_STATE["current_turn_idx"] = GAME_STATE["players"].index(winner_player)
    else:
        idx = GAME_STATE["players"].index(winner_player)
        for i in range(1, len(GAME_STATE["players"])):
            next_idx = (idx + i) % len(GAME_STATE["players"])
            if not GAME_STATE["players"][next_idx]["finished"]:
                GAME_STATE["current_turn_idx"] = next_idx
                break

def check_round_transition():
    global GAME_STATE
    if GAME_STATE["status"] != "playing":
        return
    if GAME_STATE["round_winner_id"] is not None:
        if time.time() >= GAME_STATE["round_end_time"]:
            resolve_round_transition()

# ==========================================
# DISCONNECT & LEAVE SYSTEM (Perbaikan 3)
# ==========================================
def handle_player_leave(player_id, reason="manual"):
    global GAME_STATE
    player = next((p for p in GAME_STATE["players"] if p["id"] == player_id), None)
    if not player:
        return False
        
    name = player["name"]
    
    if GAME_STATE["status"] == "waiting":
        # Jika masih di lobby, hapus langsung dari daftar
        GAME_STATE["players"] = [p for p in GAME_STATE["players"] if p["id"] != player_id]
        GAME_STATE["logs"].append(f"🔌 {name} keluar dari lobby ({reason}).")
        reassign_host_if_needed()
        return True
        
    elif GAME_STATE["status"] == "playing":
        if player.get("left", False):
            return True # Sudah ditandai keluar sebelumnya
            
        player["left"] = True
        player["finished"] = True
        
        # Buang semua sisa kartu di tangan pemain ini ke tumpukan buangan
        cards_to_discard = list(player["cards"])
        GAME_STATE["discard_pile"].extend(cards_to_discard)
        player["cards"] = []
        
        if cards_to_discard:
            log_msg = f"🔌 {name} keluar/terputus ({reason}). Kartunya dibuang ke tumpukan."
        else:
            log_msg = f"🔌 {name} keluar/terputus ({reason})."
        GAME_STATE["logs"].append(log_msg)
        
        # Jika giliran pemain ini saat keluar, geser turn ke pemain aktif berikutnya
        current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
        if current_player["id"] == player_id:
            advance_turn()
            
        # Periksa kondisi Game Over akibat pemain keluar
        active_players = [p for p in GAME_STATE["players"] if not p["finished"]]
        if len(active_players) <= 1:
            GAME_STATE["status"] = "game_over"
            if len(active_players) == 1:
                loser = active_players[0]
                loser["finished"] = True
                loser["rank"] = GAME_STATE["rank_counter"]
                GAME_STATE["logs"].append(f"💀 GAME OVER! {loser['name']} adalah sang Pecangkul (Kalah)!")
            else:
                GAME_STATE["logs"].append("🏁 Game selesai (semua pemain keluar)!")
        reassign_host_if_needed()
        return True
        
    return False

def check_and_handle_disconnects():
    global GAME_STATE
    now = time.time()
    timeout_duration = 15.0 # Batas toleransi DC: 15 detik tanpa polling status
    
    disconnected_ids = []
    for p in GAME_STATE["players"]:
        if not p.get("left", False):
            last_seen = p.get("last_seen", now)
            if (now - last_seen) > timeout_duration:
                disconnected_ids.append(p["id"])
                
    for pid in disconnected_ids:
        handle_player_leave(pid, reason="timeout")

# ==========================================
# GAME CORE ACTIONS
# ==========================================
def start_game():
    global GAME_STATE
    suits = ["S", "H", "D", "C"]
    values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    deck = [f"{s}-{v}" for s in suits for v in values]
    random.shuffle(deck)
    
    for p in GAME_STATE["players"]:
        p["cards"] = []
        p["finished"] = False
        p["rank"] = None
        p["left"] = False
        p["last_seen"] = time.time()
        
    for _ in range(7):
        for p in GAME_STATE["players"]:
            p["cards"].append(deck.pop())
            
    GAME_STATE["deck"] = deck
    GAME_STATE["discard_pile"] = []
    GAME_STATE["current_round_cards"] = []
    GAME_STATE["current_turn_idx"] = 0
    GAME_STATE["leading_suit"] = None
    GAME_STATE["round_winner_id"] = None
    GAME_STATE["round_end_time"] = 0
    GAME_STATE["rank_counter"] = 1
    GAME_STATE["status"] = "playing"
    GAME_STATE["logs"] = ["🃏 Game Dimulai! Masing-masing pemain mendapatkan 7 kartu."]
    GAME_STATE["logs"].append(f"👉 Giliran pertama: {GAME_STATE['players'][0]['name']}")

def get_client_state(player_id):
    global GAME_STATE
    players_list = []
    your_cards = []
    
    for p in GAME_STATE["players"]:
        is_you = p["id"] == player_id
        if is_you:
            your_cards = p["cards"]
        players_list.append({
            "id": p["id"],
            "name": p["name"],
            "card_count": len(p["cards"]),
            "finished": p["finished"],
            "rank": p["rank"],
            "left": p.get("left", False), # Kirim status DC ke client
            "ready": p.get("ready", False),
            "is_host": p["id"] == GAME_STATE["host_id"]
        })
        
    current_turn_name = ""
    if GAME_STATE["status"] == "playing" and len(GAME_STATE["players"]) > 0:
        current_turn_name = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]["name"]
        
    round_winner_name = None
    if GAME_STATE["round_winner_id"]:
        round_winner_name = next(p["name"] for p in GAME_STATE["players"] if p["id"] == GAME_STATE["round_winner_id"])
        
    return {
        "status": GAME_STATE["status"],
        "host_id": GAME_STATE["host_id"],
        "your_id": player_id,
        "players": players_list,
        "your_cards": your_cards,
        "current_turn": GAME_STATE["players"][GAME_STATE["current_turn_idx"]]["id"] if GAME_STATE["status"] == "playing" else None,
        "current_turn_name": current_turn_name,
        "leading_suit": GAME_STATE["leading_suit"],
        "current_round_cards": [
            {
                "player_name": next(p["name"] for p in GAME_STATE["players"] if p["id"] == item["player_id"]),
                "card": item["card"]
            } for item in GAME_STATE["current_round_cards"]
        ],
        "deck_count": len(GAME_STATE["deck"]),
        "discard_count": len(GAME_STATE["discard_pile"]),
        "round_winner_name": round_winner_name,
        "round_end_time_left": max(0, GAME_STATE["round_end_time"] - time.time()) if GAME_STATE["round_end_time"] > 0 else 0,
        "logs": GAME_STATE["logs"][-15:]
    }

# ==========================================
# MULTIPLAYER HTTP SERVER
# ==========================================
class GameRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return  # Matikan logging konsol bawaan python agar bersih

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query = urllib.parse.parse_qs(parsed_path.query)

        if path == "/api/server_info":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"ip": get_local_ip(), "port": PORT}).encode('utf-8'))
            return

        if path == "/api/status":
            player_id = query.get("player_id", [None])[0]
            
            # GET dilindungi Lock karena mengubah status "last_seen" & memicu deteksi disconnect
            with GAME_STATE_LOCK:
                if player_id:
                    for p in GAME_STATE["players"]:
                        if p["id"] == player_id:
                            p["last_seen"] = time.time()
                
                check_and_handle_disconnects()
                check_round_transition()
                state = get_client_state(player_id)
                
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(state).encode('utf-8'))
            return
            
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
            return

        # ==========================================
        # STATIC ASSET SERVING (mis. bgm4.mp3)
        # Menyajikan file yang ditaruh SEJAJAR (folder sama) dengan script ini.
        # os.path.basename() mencegah path traversal (mis. ../../rahasia.txt)
        # ==========================================
        asset_name = os.path.basename(path)
        asset_path = os.path.join(BASE_DIR, asset_name)
        if asset_name and os.path.isfile(asset_path):
            mime_type, _ = mimetypes.guess_type(asset_path)
            with open(asset_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime_type or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
            
        response_data = {"success": False, "error": "Unknown API call"}
        status_code = 400
        
        global GAME_STATE
        
        # Perbaikan 2: Membungkus aksi penulisan POST dengan Lock
        with GAME_STATE_LOCK:
            if path == "/api/join":
                name = data.get("name", "").strip()
                if not name:
                    response_data = {"success": False, "error": "Nama tidak boleh kosong"}
                elif GAME_STATE["status"] != "waiting":
                    response_data = {"success": False, "error": "Permainan sudah dimulai di lobby ini"}
                elif len(GAME_STATE["players"]) >= 7:
                    response_data = {"success": False, "error": "Lobby penuh (Max 7 pemain)"}
                else:
                    player_id = str(random.randint(100000, 999999))
                    GAME_STATE["players"].append({
                        "id": player_id,
                        "name": name,
                        "cards": [],
                        "finished": False,
                        "rank": None,
                        "last_seen": time.time(),
                        "left": False,
                        "ready": False
                    })
                    is_first_player = GAME_STATE["host_id"] is None
                    if is_first_player:
                        GAME_STATE["host_id"] = player_id
                    GAME_STATE["logs"].append(f"👋 {name} masuk ke lobby." + (" (Host)" if is_first_player else ""))
                    response_data = {"success": True, "player_id": player_id, "player_name": name, "is_host": is_first_player}
                    status_code = 200
                    
            elif path == "/api/leave": # Perbaikan 3: Endpoint leave eksplisit
                player_id = data.get("player_id")
                if player_id:
                    success = handle_player_leave(player_id, reason="keluar sendiri")
                    response_data = {"success": success}
                    status_code = 200 if success else 400
                else:
                    response_data = {"success": False, "error": "Player ID kosong"}

            elif path == "/api/start":
                player_id = data.get("player_id")
                if GAME_STATE["status"] == "playing":
                    response_data = {"success": False, "error": "Game sudah berjalan"}
                elif player_id != GAME_STATE["host_id"]:
                    response_data = {"success": False, "error": "Hanya host yang bisa memulai permainan"}
                elif len(GAME_STATE["players"]) < 2:
                    response_data = {"success": False, "error": "Butuh minimal 2 pemain untuk bermain"}
                else:
                    start_game()
                    response_data = {"success": True}
                    status_code = 200

            elif path == "/api/toggle_ready":
                player_id = data.get("player_id")
                if GAME_STATE["status"] != "waiting":
                    response_data = {"success": False, "error": "Tidak bisa mengubah status siap saat ini"}
                else:
                    player = next((p for p in GAME_STATE["players"] if p["id"] == player_id), None)
                    if not player:
                        response_data = {"success": False, "error": "Pemain tidak ditemukan"}
                    else:
                        player["ready"] = not player["ready"]
                        GAME_STATE["logs"].append(f"{'✅' if player['ready'] else '⏸️'} {player['name']} {'siap bermain' if player['ready'] else 'membatalkan status siap'}.")
                        response_data = {"success": True, "ready": player["ready"]}
                        status_code = 200
                    
            elif path == "/api/play_card":
                player_id = data.get("player_id")
                card = data.get("card")
                
                if GAME_STATE["status"] != "playing" or GAME_STATE["round_winner_id"] is not None:
                    response_data = {"success": False, "error": "Game tidak dalam kondisi menerima kartu"}
                else:
                    current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                    if current_player["id"] != player_id:
                        response_data = {"success": False, "error": "Bukan giliran Anda"}
                    elif card not in current_player["cards"]:
                        response_data = {"success": False, "error": "Kartu tidak ada di tangan Anda"}
                    else:
                        suit, _ = card.split("-")
                        
                        if GAME_STATE["leading_suit"] is None:
                            GAME_STATE["leading_suit"] = suit
                            GAME_STATE["current_round_cards"].append({"player_id": player_id, "card": card})
                            current_player["cards"].remove(card)
                            GAME_STATE["logs"].append(f"📤 {current_player['name']} membuang {card} (Simbol wajib: {suit})")
                            advance_turn()
                            response_data = {"success": True}
                            status_code = 200
                        else:
                            has_suit = player_has_suit(current_player, GAME_STATE["leading_suit"])
                            if has_suit and suit != GAME_STATE["leading_suit"]:
                                response_data = {"success": False, "error": f"Anda harus membuang kartu bersimbol {GAME_STATE['leading_suit']}!"}
                            elif not has_suit and suit != GAME_STATE["leading_suit"]:
                                response_data = {"success": False, "error": "Kartu tidak cocok. Silakan cangkul sampai dapat simbol yang sesuai!"}
                            else:
                                GAME_STATE["current_round_cards"].append({"player_id": player_id, "card": card})
                                current_player["cards"].remove(card)
                                GAME_STATE["logs"].append(f"📥 {current_player['name']} membuang {card}")
                                
                                if is_round_complete():
                                    winner_id, winner_card = determine_round_winner()
                                    winner_name = next(p["name"] for p in GAME_STATE["players"] if p["id"] == winner_id)
                                    GAME_STATE["round_winner_id"] = winner_id
                                    GAME_STATE["round_end_time"] = time.time() + 4.0
                                    GAME_STATE["logs"].append(f"⭐ {winner_name} memenangkan ronde ini dengan kartu {winner_card}!")
                                else:
                                    advance_turn()
                                    
                                response_data = {"success": True}
                                status_code = 200
                                
            elif path == "/api/draw_card":
                player_id = data.get("player_id")
                if GAME_STATE["status"] != "playing" or GAME_STATE["round_winner_id"] is not None:
                    response_data = {"success": False, "error": "Tidak bisa mengambil kartu saat ini"}
                else:
                    current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                    if current_player["id"] != player_id:
                        response_data = {"success": False, "error": "Bukan giliran Anda"}
                    elif GAME_STATE["leading_suit"] is None:
                        response_data = {"success": False, "error": "Anda bebas membuang kartu apa saja, tidak perlu mencangkul"}
                    elif player_has_suit(current_player, GAME_STATE["leading_suit"]):
                        response_data = {"success": False, "error": "Anda masih memiliki kartu dengan simbol wajib di tangan!"}
                    else:
                        drawn = draw_card_from_deck()
                        if drawn:
                            current_player["cards"].append(drawn)
                            GAME_STATE["logs"].append(f"🎣 {current_player['name']} mengambil kartu dari tumpukan (Cangkul).")
                            response_data = {"success": True, "drawn_card": drawn}
                            status_code = 200
                        else:
                            penalty_cards = [item["card"] for item in GAME_STATE["current_round_cards"]]
                            leader_id = GAME_STATE["current_round_cards"][0]["player_id"]

                            current_player["cards"].extend(penalty_cards)
                            GAME_STATE["logs"].append(
                                f"💥 Tumpukan cangkul & buangan habis! {current_player['name']} tidak menemukan kartu "
                                f"bersimbol {GAME_STATE['leading_suit']} dan wajib mengambil semua {len(penalty_cards)} kartu di meja!"
                            )

                            GAME_STATE["current_round_cards"] = []
                            GAME_STATE["leading_suit"] = None
                            leader_idx = next(i for i, p in enumerate(GAME_STATE["players"]) if p["id"] == leader_id)
                            GAME_STATE["current_turn_idx"] = leader_idx

                            leader_name = GAME_STATE["players"][leader_idx]["name"]
                            GAME_STATE["logs"].append(f"🔁 Ronde dibatalkan. Giliran kembali ke {leader_name} untuk memimpin ronde baru.")

                            response_data = {
                                "success": True,
                                "drawn_card": None,
                                "penalty": True,
                                "cards_taken": len(penalty_cards)
                            }
                            status_code = 200
                            
            elif path == "/api/reset":
                player_id = data.get("player_id")
                if player_id != GAME_STATE["host_id"]:
                    response_data = {"success": False, "error": "Hanya host yang bisa mereset permainan"}
                else:
                    GAME_STATE = {
                        "status": "waiting",
                        "players": [],
                        "deck": [],
                        "discard_pile": [],
                        "current_round_cards": [],
                        "current_turn_idx": 0,
                        "leading_suit": None,
                        "logs": ["♻️ Lobby di-reset oleh host."],
                        "round_winner_id": None,
                        "round_end_time": 0,
                        "rank_counter": 1,
                        "host_id": None
                    }
                    response_data = {"success": True}
                    status_code = 200
                
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(response_data).encode('utf-8'))

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

# ==========================================
# HTML, CSS & JAVASCRIPT GAME INTERFACE
# ==========================================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kartu Cangkulan Local Multiplayer</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {
            background-color: #0f172a;
            color: #f1f5f9;
        }
        .wood-table {
            background: radial-gradient(circle, #0f5132 0%, #082d1c 100%);
            box-shadow: inset 0 0 100px rgba(0,0,0,0.8);
        }
    </style>
</head>
<body class="min-h-screen flex flex-col font-sans select-none">

    <div id="join-modal" class="fixed inset-0 bg-slate-950/90 flex items-center justify-center z-50">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-3xl shadow-2xl max-w-md w-full mx-4 text-center">
            <h1 class="text-4xl font-extrabold mb-2 text-yellow-400 tracking-wide flex items-center justify-center gap-2">
                <span>🃏</span> CANGKULAN
            </h1>
            <p class="text-slate-400 text-sm mb-6">Game Kartu Remi Tradisional Multiplayer Jaringan Lokal</p>
            
            <div id="join-form-container">
                <div class="mb-6 text-left">
                    <label class="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">Nama Pemain (Maks 12 Huruf)</label>
                    <input type="text" id="join-name" maxlength="12" class="w-full px-4 py-3 bg-slate-800 border border-slate-700 text-white rounded-xl focus:outline-none focus:ring-2 focus:ring-yellow-500 font-bold text-center text-lg" placeholder="Masukkan nama Anda...">
                </div>
                <button onclick="joinGame()" class="w-full bg-yellow-500 hover:bg-yellow-600 text-slate-950 font-black py-4 rounded-xl transition duration-200 text-lg shadow-lg active:scale-95 transform">
                    MASUK LOBBY
                </button>
            </div>

            <div id="join-wait-container" class="hidden py-4">
                <div class="flex flex-col items-center gap-4">
                    <span class="text-5xl animate-bounce">⏳</span>
                    <h3 class="text-lg font-black text-yellow-400">Permainan Sedang Berjalan...</h3>
                    <p class="text-slate-400 text-xs px-4">Lobby saat ini sedang ditutup karena ronde sedang berlangsung. Layar ini akan kembali memunculkan kolom nama secara otomatis jika game sudah selesai atau di-reset.</p>
                </div>
            </div>
            <p class="text-center text-[10px] text-slate-600 mt-6">Developers By Dhamas and Teams</p>
        </div>
    </div>

    <div id="lobby-screen" class="hidden max-w-md mx-auto mt-16 bg-slate-900 border border-slate-800 p-6 rounded-3xl shadow-xl w-full">
        <h2 class="text-2xl font-bold text-yellow-400 mb-4 text-center">Lobby Ruang Tunggu</h2>
        <div class="bg-slate-950 rounded-2xl p-4 mb-6">
            <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Pemain Bergabung:</h3>
            <ul id="lobby-players-list" class="space-y-2"></ul>
        </div>
        <div class="space-y-3">
            <button onclick="copyInviteLink()" class="w-full bg-slate-800 hover:bg-slate-700 font-bold py-3 rounded-xl transition flex items-center justify-center gap-2">
                <span>🔗</span> Salin Link Undangan Jaringan
            </button>
            <button id="start-game-btn" onclick="startGame()" class="hidden w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-black py-4 rounded-xl transition text-lg shadow-lg active:scale-95 transform">
                MULAI PERMAINAN
            </button>
            <button id="ready-toggle-btn" onclick="toggleReady()" class="hidden w-full font-black py-4 rounded-xl transition text-lg shadow-lg active:scale-95 transform">
                SIAP
            </button>
            <p id="waiting-host-msg" class="hidden text-center text-xs text-slate-400 font-bold animate-pulse">⏳ Menunggu host memulai permainan...</p>
        </div>
        <p id="min-player-hint" class="text-[10px] text-slate-500 text-center mt-4">Minimal membutuhkan 2 pemain untuk memulai.</p>
        <p class="text-center text-[10px] text-slate-600 mt-2">Developers By Dhamas and Teams</p>
    </div>

    <div id="game-screen" class="hidden max-w-6xl mx-auto w-full px-4 py-4 flex-1 flex flex-col lg:grid lg:grid-cols-4 gap-6">
        
        <div class="lg:col-span-1 bg-slate-900 border border-slate-800 rounded-3xl p-4 flex flex-col justify-between max-h-[600px] lg:max-h-none">
            <div>
                <div class="flex justify-between items-center mb-4 pb-2 border-b border-slate-800">
                    <h3 class="font-bold text-slate-300">👥 Pemain</h3>
                    <span id="player-count-badge" class="bg-slate-800 text-slate-400 text-xs px-2 py-0.5 rounded-full font-bold">0/7</span>
                </div>
                <div id="players-game-list" class="space-y-2 max-h-[200px] overflow-y-auto lg:max-h-none"></div>
            </div>
            
            <div class="mt-4 flex-1 flex flex-col justify-end min-h-[120px]">
                <h4 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">Aktivitas Permainan</h4>
                <div id="game-logs" class="bg-slate-950/80 rounded-2xl p-3 border border-slate-800/50 text-xs font-mono space-y-1 h-36 overflow-y-auto"></div>
            </div>
            
            <div class="mt-4 space-y-2">
                <button onclick="leaveGame()" class="w-full bg-slate-800 hover:bg-slate-700 text-slate-300 font-bold py-2 rounded-xl text-xs transition">
                    🚪 Keluar Permainan (Leave)
                </button>
                <button id="ingame-reset-btn" onclick="resetGame()" class="hidden w-full bg-red-950/40 hover:bg-red-900/40 text-red-400 border border-red-900/30 font-bold py-2 rounded-xl text-xs transition">
                    ♻️ Reset Game & Lobby
                </button>
                <p class="text-center text-[10px] text-slate-600 pt-2">Developers By Dhamas and Teams</p>
            </div>
        </div>

        <div class="lg:col-span-3 flex flex-col gap-6">
            
            <div id="status-bar" class="bg-slate-900 border border-slate-800 rounded-2xl p-4 flex justify-between items-center">
                <div class="flex items-center gap-3">
                    <span id="status-turn-icon" class="text-3xl">⏳</span>
                    <div>
                        <h2 id="status-message" class="font-black text-lg text-yellow-400">Menghubungkan...</h2>
                        <p id="status-submessage" class="text-xs text-slate-400">Mempersiapkan ronde permainan</p>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="SoundFX.toggleMusic()" id="btn-music" class="bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-xl text-xs font-bold transition">
                        🔇 Musik (Mati)
                    </button>
                    <button onclick="toggleSfx()" id="btn-sfx" class="bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-xl text-xs font-bold transition">
                        🔊 Sfx (Aktif)
                    </button>
                </div>
            </div>

            <div class="wood-table rounded-[2.5rem] border-[6px] border-slate-950 p-6 flex flex-col justify-between min-h-[400px] relative">
                
                <div class="flex-1 flex flex-col justify-center items-center gap-4">
                    <div class="text-center bg-slate-950/40 px-4 py-2 rounded-2xl backdrop-blur-sm">
                        <span class="text-[10px] uppercase font-black tracking-wider text-green-300">Simbol Wajib Ronde Ini</span>
                        <div id="leading-suit-display" class="text-5xl font-black text-white mt-1">-</div>
                    </div>
                    
                    <div class="w-full">
                        <div id="table-cards-container" class="flex flex-wrap justify-center gap-4 items-center min-h-[150px]"></div>
                    </div>
                </div>

                <div class="flex justify-between items-end">
                    
                    <div class="flex flex-col items-center gap-1">
                        <div class="bg-green-950/40 border-2 border-dashed border-green-800/40 rounded-2xl w-20 h-28 flex flex-col items-center justify-center text-green-600/60 shadow-inner">
                            <span class="text-[9px] font-extrabold uppercase">Buangan</span>
                            <span id="discard-count" class="text-2xl font-black">0</span>
                        </div>
                    </div>

                    <div class="flex flex-col items-center gap-1">
                        <div id="deck-card" onclick="drawCard()" class="bg-indigo-900 border-2 border-indigo-600 rounded-2xl w-20 h-28 flex flex-col items-center justify-center shadow-2xl cursor-pointer transform transition duration-150 active:scale-95 relative overflow-hidden group">
                            <div class="absolute inset-1 border border-dashed border-indigo-400/50 rounded-xl flex flex-col items-center justify-center text-white">
                                <span class="text-[9px] font-black uppercase tracking-wider mb-1">CANGKUL</span>
                                <span id="deck-count" class="text-3xl font-black">0</span>
                            </div>
                        </div>
                        <span id="draw-arrow" class="hidden text-xs text-yellow-400 font-black animate-bounce mt-1">⬇️ AMBIL KARTU</span>
                    </div>

                </div>

            </div>

            <div class="bg-slate-900 border border-slate-800 rounded-3xl p-6">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-sm font-bold uppercase tracking-wider text-slate-400">🃏 Kartu Tangan Anda</h3>
                    <span id="hand-count-badge" class="bg-slate-800 text-yellow-400 text-xs px-3 py-1 rounded-full font-black border border-slate-700/50">0 Kartu</span>
                </div>
                <div id="your-hand-container" class="flex flex-wrap justify-center gap-3 min-h-[140px] py-2"></div>
            </div>

        </div>

    </div>

    <div id="game-over-modal" class="hidden fixed inset-0 bg-slate-950/95 flex items-center justify-center z-50">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-[2rem] shadow-2xl max-w-md w-full mx-4 text-center">
            <h2 class="text-4xl font-black text-red-500 mb-2">🏁 SELESAI!</h2>
            <p class="text-slate-400 text-sm mb-6">Permainan telah berakhir.</p>
            
            <div class="bg-slate-950 rounded-2xl p-4 mb-6 text-left border border-slate-800">
                <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Papan Peringkat Akhir:</h3>
                <div id="game-results-list" class="space-y-2"></div>
            </div>
            
            <button id="gameover-reset-btn" onclick="resetGame()" class="hidden w-full bg-yellow-500 hover:bg-yellow-600 text-slate-950 font-black py-4 rounded-xl transition text-lg shadow-lg">
                MULAI LOBBY BARU
            </button>
            <p id="gameover-waiting-host-msg" class="hidden text-center text-xs text-slate-400 font-bold animate-pulse">⏳ Menunggu host mereset lobby...</p>
            <p class="text-center text-[10px] text-slate-600 mt-4">Developers By Dhamas and Teams</p>
        </div>
    </div>

    <script>
        const SoundFX = {
            ctx: null,
            musicEnabled: false,
            soundEnabled: true,
            bgmAudio: null,

            init() {
                if (!this.ctx) {
                    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
                }
                if (!this.bgmAudio) {
                    // BGM sekarang file audio eksternal (bgm4.mp3), taruh sejajar dengan server_cangkulan.py
                    this.bgmAudio = new Audio("/bgm4.mp3");
                    this.bgmAudio.loop = true;
                    this.bgmAudio.volume = 0.4;
                }
            },

            playCardPlace() {
                if (!this.soundEnabled) return;
                this.init();
                const now = this.ctx.currentTime;
                
                const osc = this.ctx.createOscillator();
                const gainOsc = this.ctx.createGain();
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(140, now);
                osc.frequency.exponentialRampToValueAtTime(30, now + 0.12);
                
                gainOsc.gain.setValueAtTime(0.6, now);
                gainOsc.gain.exponentialRampToValueAtTime(0.01, now + 0.12);
                
                osc.connect(gainOsc);
                gainOsc.connect(this.ctx.destination);
                
                const bufferSize = this.ctx.sampleRate * 0.06;
                const buffer = this.ctx.createBuffer(1, bufferSize, this.ctx.sampleRate);
                const data = buffer.getChannelData(0);
                for (let i = 0; i < bufferSize; i++) {
                    data[i] = Math.random() * 2 - 1;
                }
                
                const noise = this.ctx.createBufferSource();
                noise.buffer = buffer;
                
                const noiseFilter = this.ctx.createBiquadFilter();
                noiseFilter.type = 'bandpass';
                noiseFilter.frequency.value = 1200;
                
                const gainNoise = this.ctx.createGain();
                gainNoise.gain.setValueAtTime(0.25, now);
                gainNoise.gain.exponentialRampToValueAtTime(0.01, now + 0.06);
                
                noise.connect(noiseFilter);
                noiseFilter.connect(gainNoise);
                gainNoise.connect(this.ctx.destination);
                
                osc.start(now);
                osc.stop(now + 0.12);
                noise.start(now);
                noise.stop(now + 0.06);
            },

            playShuffle() {
                if (!this.soundEnabled) return;
                this.init();
                const now = this.ctx.currentTime;
                
                const bursts = 10;
                for (let i = 0; i < bursts; i++) {
                    const burstTime = now + (i * 0.08);
                    const duration = 0.06;
                    
                    const bufferSize = this.ctx.sampleRate * duration;
                    const buffer = this.ctx.createBuffer(1, bufferSize, this.ctx.sampleRate);
                    const data = buffer.getChannelData(0);
                    for (let j = 0; j < bufferSize; j++) {
                        data[j] = Math.random() * 2 - 1;
                    }
                    
                    const noise = this.ctx.createBufferSource();
                    noise.buffer = buffer;
                    
                    const filter = this.ctx.createBiquadFilter();
                    filter.type = 'lowpass';
                    filter.frequency.setValueAtTime(1500, burstTime);
                    filter.frequency.exponentialRampToValueAtTime(250, burstTime + duration);
                    
                    const gain = this.ctx.createGain();
                    gain.gain.setValueAtTime(0.18, burstTime);
                    gain.gain.exponentialRampToValueAtTime(0.01, burstTime + duration);
                    
                    noise.connect(filter);
                    filter.connect(gain);
                    gain.connect(this.ctx.destination);
                    
                    noise.start(burstTime);
                    noise.stop(burstTime + duration);
                }
            },

            toggleMusic() {
                this.init();
                const btn = document.getElementById("btn-music");
                if (this.musicEnabled) {
                    this.musicEnabled = false;
                    btn.textContent = "🔇 Musik (Mati)";
                    btn.className = "bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-xl text-xs font-bold transition";
                    this.bgmAudio.pause();
                } else {
                    this.musicEnabled = true;
                    btn.textContent = "🎵 Musik (Aktif)";
                    btn.className = "bg-yellow-500 hover:bg-yellow-600 text-slate-950 px-3 py-2 rounded-xl text-xs font-black transition";
                    this.bgmAudio.play().catch(err => console.warn("Autoplay BGM diblokir browser:", err));
                }
            }
        };

        function toggleSfx() {
            SoundFX.soundEnabled = !SoundFX.soundEnabled;
            const btn = document.getElementById("btn-sfx");
            if (SoundFX.soundEnabled) {
                btn.textContent = "🔊 Sfx (Aktif)";
                btn.className = "bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-xl text-xs font-bold transition";
            } else {
                btn.textContent = "🔇 Sfx (Mati)";
                btn.className = "bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-xl text-xs font-bold transition opacity-60";
            }
        }

        document.body.addEventListener('click', () => {
            SoundFX.init();
        }, { once: true });
    </script>

    <script>
        let playerId = localStorage.getItem("cangkul_player_id") || null;
        let playerName = localStorage.getItem("cangkul_player_name") || null;
        let gameState = null;
        let prevState = null;
        let pollInterval = null;

        const API_URL = ""; 

        async function apiPost(endpoint, body = {}) {
            try {
                const res = await fetch(API_URL + endpoint, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body)
                });
                return await res.json();
            } catch (e) {
                console.error(e);
                return { success: false, error: "Gagal terhubung dengan server jaringan." };
            }
        }

        async function apiGet(endpoint) {
            try {
                const res = await fetch(API_URL + endpoint);
                return await res.json();
            } catch (e) {
                console.error(e);
                return null;
            }
        }

        async function joinGame() {
            const input = document.getElementById("join-name");
            const name = input.value.trim();
            if (!name) return alert("Silakan isi nama Anda terlebih dahulu!");
            
            const res = await apiPost("/api/join", { name });
            if (res.success) {
                playerId = res.player_id;
                playerName = res.player_name;
                localStorage.setItem("cangkul_player_id", playerId);
                localStorage.setItem("cangkul_player_name", playerName);
                
                document.getElementById("join-modal").classList.add("hidden");
                startPolling();
            } else {
                alert("Kesalahan: " + res.error);
            }
        }

        // Perbaikan 3: Fungsi Leave Manual
        async function leaveGame() {
            if (confirm("Apakah Anda yakin ingin keluar dari permainan ini?")) {
                if (playerId) {
                    await apiPost("/api/leave", { player_id: playerId });
                    playerId = null;
                    playerName = null;
                    localStorage.clear();
                    location.reload();
                }
            }
        }

        // Perbaikan 3: Kirim sinyal leave secara instan sebelum tab di-close
        window.addEventListener("beforeunload", () => {
            if (playerId) {
                const data = JSON.stringify({ player_id: playerId });
                navigator.sendBeacon("/api/leave", data);
            }
        });

        async function startGame() {
            const res = await apiPost("/api/start", { player_id: playerId });
            if (!res.success) {
                alert("Gagal memulai game: " + res.error);
            }
        }

        async function toggleReady() {
            const res = await apiPost("/api/toggle_ready", { player_id: playerId });
            if (!res.success) {
                alert("Gagal mengubah status siap: " + res.error);
            } else {
                pollState();
            }
        }

        async function playCard(card) {
            const res = await apiPost("/api/play_card", { player_id: playerId, card });
            if (!res.success) {
                alert(res.error);
            } else {
                pollState();
            }
        }

        async function drawCard() {
            const res = await apiPost("/api/draw_card", { player_id: playerId });
            if (!res.success) {
                alert(res.error);
            } else {
                if (res.penalty) {
                    SoundFX.playShuffle();
                    alert(`💥 Tumpukan cangkul & buangan habis!\\nAnda tidak menemukan kartu yang cocok dan wajib mengambil ${res.cards_taken} kartu di meja.\\nRonde dibatalkan, giliran kembali ke pemimpin ronde ini.`);
                }
                pollState();
            }
        }

        async function resetGame() {
            if (confirm("Apakah Anda yakin ingin mengatur ulang (reset) permainan ini kembali ke lobby?")) {
                const res = await apiPost("/api/reset", { player_id: playerId });
                if (res.success) {
                    pollState();
                } else if (res.error) {
                    alert(res.error);
                }
            }
        }

        async function copyInviteLink() {
            let link = window.location.origin;
            try {
                const res = await fetch("/api/server_info");
                const info = await res.json();
                link = `http://${info.ip}:${info.port}`;
            } catch (e) {
                // fallback ke window.location.origin jika endpoint gagal diakses
            }
            navigator.clipboard.writeText(link).then(() => {
                alert("Link Undangan berhasil disalin!\\nBagikan URL ini ke teman Anda yang terhubung di satu jaringan Wi-Fi:\\n" + link);
            }).catch(() => {
                alert("Salin URL browser Anda: " + link);
            });
        }

        function createCardHTML(cardStr, clickHandler = '', isPlayable = true) {
            const [suit, val] = cardStr.split("-");
            const suitMap = { "S": "♠", "H": "♥", "D": "♦", "C": "♣" };
            
            const isRed = (suit === "H" || suit === "D");
            const colorClass = isRed ? "text-red-500 border-red-900/40 bg-slate-950/80" : "text-slate-200 border-slate-800 bg-slate-950/80";
            const icon = suitMap[suit];
            
            const onclickAttr = clickHandler ? `onclick="${clickHandler}"` : '';
            const actionClass = isPlayable 
                ? 'hover:-translate-y-4 cursor-pointer border-yellow-500 shadow-yellow-500/10 ring-2 ring-yellow-500/20 active:scale-95' 
                : 'opacity-50 border-slate-900 filter saturate-50 pointer-events-none';
                
            return `
                <div ${onclickAttr} class="relative border-2 rounded-2xl p-3 flex flex-col justify-between w-20 h-28 select-none transform transition-all duration-200 shadow-md ${colorClass} ${actionClass}">
                    <div class="flex justify-between items-start leading-none">
                        <span class="text-xs font-black">${val}</span>
                        <span class="text-[10px]">${icon}</span>
                    </div>
                    <div class="text-3xl text-center font-extrabold leading-none">${icon}</div>
                    <div class="flex justify-between items-end transform rotate-180 leading-none">
                        <span class="text-xs font-black">${val}</span>
                        <span class="text-[10px]">${icon}</span>
                    </div>
                </div>
            `;
        }

        function renderLobby(state) {
            document.getElementById("lobby-screen").classList.remove("hidden");
            document.getElementById("game-screen").classList.add("hidden");
            document.getElementById("game-over-modal").classList.add("hidden");
            
            const isHost = state.host_id === playerId;
            
            const list = document.getElementById("lobby-players-list");
            list.innerHTML = "";
            state.players.forEach(p => {
                const isMe = p.id === playerId;
                const statusBadge = p.is_host
                    ? '<span class="text-xs text-yellow-400 font-bold">👑 Host</span>'
                    : (p.ready
                        ? '<span class="text-xs text-emerald-400 font-bold">✅ Siap</span>'
                        : '<span class="text-xs text-slate-500 font-bold">⏸️ Belum siap</span>');
                list.innerHTML += `
                    <li class="flex justify-between items-center bg-slate-800/40 border border-slate-800 p-3 rounded-xl">
                        <span class="font-bold flex items-center gap-2">
                            <span>👤</span> ${p.name} ${isMe ? '<span class="text-[10px] bg-yellow-500/20 text-yellow-400 border border-yellow-500/30 px-2 py-0.5 rounded-full">Anda</span>' : ''}
                        </span>
                        ${statusBadge}
                    </li>
                `;
            });
            
            const startBtn = document.getElementById("start-game-btn");
            const readyBtn = document.getElementById("ready-toggle-btn");
            const waitingMsg = document.getElementById("waiting-host-msg");
            const minHint = document.getElementById("min-player-hint");
            
            if (isHost) {
                startBtn.classList.remove("hidden");
                readyBtn.classList.add("hidden");
                waitingMsg.classList.add("hidden");
                minHint.classList.remove("hidden");
                
                if (state.players.length < 2) {
                    startBtn.disabled = true;
                    startBtn.classList.add("opacity-50", "cursor-not-allowed");
                } else {
                    startBtn.disabled = false;
                    startBtn.classList.remove("opacity-50", "cursor-not-allowed");
                }
            } else {
                startBtn.classList.add("hidden");
                readyBtn.classList.remove("hidden");
                waitingMsg.classList.remove("hidden");
                minHint.classList.add("hidden");
                
                const me = state.players.find(p => p.id === playerId);
                const iAmReady = me ? me.ready : false;
                if (iAmReady) {
                    readyBtn.textContent = "BATAL";
                    readyBtn.className = "w-full bg-slate-700 hover:bg-slate-600 text-slate-200 font-black py-4 rounded-xl transition text-lg shadow-lg active:scale-95 transform";
                } else {
                    readyBtn.textContent = "SIAP";
                    readyBtn.className = "w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-black py-4 rounded-xl transition text-lg shadow-lg active:scale-95 transform";
                }
            }
        }

        function renderGame(state) {
            document.getElementById("lobby-screen").classList.add("hidden");
            document.getElementById("game-screen").classList.remove("hidden");
            document.getElementById("game-over-modal").classList.add("hidden");
            
            const ingameResetBtn = document.getElementById("ingame-reset-btn");
            if (state.host_id === playerId) {
                ingameResetBtn.classList.remove("hidden");
            } else {
                ingameResetBtn.classList.add("hidden");
            }
            
            document.getElementById("player-count-badge").textContent = `${state.players.length}/7`;
            
            const playersGameList = document.getElementById("players-game-list");
            playersGameList.innerHTML = "";
            state.players.forEach(p => {
                const isCurrentTurn = state.current_turn === p.id;
                const isMe = p.id === playerId;
                
                let badge = "";
                if (p.left) { // Perbaikan 3: Desain Tag LEFT/DC untuk Pemain Terputus
                    badge = `<span class="bg-red-500/20 text-red-400 text-[10px] font-bold border border-red-500/30 px-2 py-0.5 rounded">DC / LEFT</span>`;
                } else if (p.finished) {
                    badge = `<span class="bg-emerald-500/20 text-emerald-400 text-[10px] font-bold border border-emerald-500/30 px-2 py-0.5 rounded">Rank ${p.rank}</span>`;
                } else if (isCurrentTurn) {
                    badge = `<span class="bg-yellow-500/20 text-yellow-400 text-[10px] font-bold border border-yellow-500/30 px-2 py-0.5 rounded animate-pulse">GILIRAN</span>`;
                } else {
                    badge = `<span class="bg-slate-800 text-slate-300 text-[10px] border border-slate-700 px-2 py-0.5 rounded font-bold">${p.card_count} Kartu</span>`;
                }
                
                playersGameList.innerHTML += `
                    <div class="flex justify-between items-center p-3 rounded-xl border transition ${p.left ? 'opacity-50' : ''} ${isCurrentTurn ? 'bg-yellow-500/5 border-yellow-500/30' : 'bg-slate-950/40 border-slate-800/80'}">
                        <span class="font-bold text-sm truncate flex items-center gap-1.5 ${isCurrentTurn ? 'text-yellow-400' : 'text-slate-300'}">
                            <span>${isCurrentTurn ? '👉' : '👤'}</span>
                            <span class="max-w-[100px] truncate">${p.name} ${isMe ? '<span class="text-[9px] text-blue-400">(Anda)</span>' : ''}</span>
                        </span>
                        <div>${badge}</div>
                    </div>
                `;
            });
            
            const logsBox = document.getElementById("game-logs");
            logsBox.innerHTML = "";
            state.logs.forEach(log => {
                logsBox.innerHTML += `<div class="py-0.5 border-b border-slate-900 text-slate-300 font-bold">${log}</div>`;
            });
            logsBox.scrollTop = logsBox.scrollHeight;
            
            document.getElementById("deck-count").textContent = state.deck_count;
            document.getElementById("discard-count").textContent = state.discard_count;
            
            const suitDisplay = document.getElementById("leading-suit-display");
            const suitNameMap = { "S": "Sekop ♠", "H": "Hati ♥", "D": "Wajik ♦", "C": "Keriting ♣" };
            const suitColors = { "S": "text-slate-300", "H": "text-red-500", "D": "text-red-500", "C": "text-slate-300" };
            const suitSymbols = { "S": "♠", "H": "♥", "D": "♦", "C": "♣" };
            
            if (state.leading_suit) {
                suitDisplay.textContent = suitSymbols[state.leading_suit];
                suitDisplay.className = "text-5xl font-black mt-1 " + suitColors[state.leading_suit];
                document.getElementById("status-submessage").textContent = `Pemain wajib membuang kartu bersimbol ${suitNameMap[state.leading_suit]}`;
            } else {
                suitDisplay.textContent = "—";
                suitDisplay.className = "text-5xl font-black text-slate-600 mt-1";
                document.getElementById("status-submessage").textContent = "Ronde Baru! Bebas membuang kartu bersimbol apa saja.";
            }
            
            const tableContainer = document.getElementById("table-cards-container");
            tableContainer.innerHTML = "";
            if (state.current_round_cards.length === 0) {
                tableContainer.innerHTML = `
                    <div class="text-xs font-bold text-slate-500 border-2 border-dashed border-slate-800 rounded-3xl p-6 text-center w-full max-w-sm">
                        Meja kosong. Giliran membuang kartu pertama!
                    </div>
                `;
            } else {
                state.current_round_cards.forEach(item => {
                    const cardHtml = createCardHTML(item.card, "", false);
                    tableContainer.innerHTML += `
                        <div class="flex flex-col items-center gap-1.5 animate-fade-in">
                            ${cardHtml}
                            <span class="text-[10px] bg-slate-950 border border-slate-800 text-yellow-400 font-black px-2 py-0.5 rounded-full truncate max-w-[80px]">${item.player_name}</span>
                        </div>
                    `;
                });
            }
            
            const isMyTurn = state.current_turn === playerId;
            const hasLeadingSuit = state.leading_suit 
                ? state.your_cards.some(c => c.split("-")[0] === state.leading_suit)
                : true;
                
            const drawArrow = document.getElementById("draw-arrow");
            const deckCard = document.getElementById("deck-card");
            
            if (isMyTurn && state.leading_suit && !hasLeadingSuit) {
                drawArrow.classList.remove("hidden");
                deckCard.classList.add("ring-4", "ring-yellow-500", "animate-pulse");
            } else {
                drawArrow.classList.add("hidden");
                deckCard.classList.remove("ring-4", "ring-yellow-500", "animate-pulse");
            }
            
            const handContainer = document.getElementById("your-hand-container");
            handContainer.innerHTML = "";
            document.getElementById("hand-count-badge").textContent = `${state.your_cards.length} Kartu`;
            
            state.your_cards.forEach(card => {
                let isPlayable = false;
                if (isMyTurn && !state.round_winner_name) {
                    if (!state.leading_suit) {
                        isPlayable = true;
                    } else {
                        isPlayable = (card.split("-")[0] === state.leading_suit);
                    }
                }
                const handler = isPlayable ? `playCard('${card}')` : '';
                handContainer.innerHTML += createCardHTML(card, handler, isPlayable);
            });
            
            const turnIcon = document.getElementById("status-turn-icon");
            const mainMsg = document.getElementById("status-message");
            
            if (state.round_winner_name) {
                turnIcon.textContent = "👑";
                mainMsg.textContent = `${state.round_winner_name} Memenangkan Ronde Ini!`;
                mainMsg.className = "font-black text-lg text-yellow-400";
                document.getElementById("status-submessage").innerHTML = `
                    <span class="text-slate-400 animate-pulse font-bold">Ronde baru akan dimulai otomatis dalam ${Math.ceil(state.round_end_time_left)} detik...</span>
                `;
            } else if (isMyTurn) {
                turnIcon.textContent = "⚡";
                mainMsg.textContent = "Giliran Anda!";
                mainMsg.className = "font-black text-lg text-yellow-400 animate-pulse";
            } else {
                turnIcon.textContent = "⏳";
                mainMsg.textContent = `Menunggu giliran ${state.current_turn_name}...`;
                mainMsg.className = "font-black text-lg text-slate-300";
            }
        }

        function renderGameOver(state) {
            document.getElementById("game-over-modal").classList.remove("hidden");
            
            const gameoverResetBtn = document.getElementById("gameover-reset-btn");
            const gameoverWaitingMsg = document.getElementById("gameover-waiting-host-msg");
            if (state.host_id === playerId) {
                gameoverResetBtn.classList.remove("hidden");
                gameoverWaitingMsg.classList.add("hidden");
            } else {
                gameoverResetBtn.classList.add("hidden");
                gameoverWaitingMsg.classList.remove("hidden");
            }
            
            const list = document.getElementById("game-results-list");
            list.innerHTML = "";
            
            const sorted = [...state.players].sort((a, b) => (a.rank || 99) - (b.rank || 99));
            sorted.forEach(p => {
                const isLoser = p.rank === state.players.length;
                list.innerHTML += `
                    <div class="flex justify-between items-center p-3 border rounded-xl ${isLoser ? 'bg-red-950/20 border-red-900/40 text-red-400 font-black' : 'bg-slate-900 border-slate-800 text-slate-200'}">
                        <span class="flex items-center gap-2">
                            <span>${isLoser ? '💀 Pecangkul' : '🏆 Rank ' + p.rank}</span>
                            <span>${p.name}</span>
                        </span>
                        <span class="text-xs uppercase">${isLoser ? 'KALAH TOTAL' : 'AMAN'}</span>
                    </div>
                `;
            });
        }

        function renderState(state) {
            if (!state) return;
            gameState = state;
            
            const exists = state.players.some(p => p.id === playerId);
            if (!exists && playerId !== null) {
                playerId = null;
                playerName = null;
                localStorage.clear();
                document.getElementById("join-modal").classList.remove("hidden");
                return;
            }
            
            // Perbaikan 4: Logika UX Layar Antrean Tunggu otomatis
            if (playerId === null) {
                document.getElementById("join-modal").classList.remove("hidden");
                document.getElementById("lobby-screen").classList.add("hidden");
                document.getElementById("game-screen").classList.add("hidden");
                document.getElementById("game-over-modal").classList.add("hidden");
                
                const formCont = document.getElementById("join-form-container");
                const waitCont = document.getElementById("join-wait-container");
                
                if (state.status === "playing") {
                    formCont.classList.add("hidden");
                    waitCont.classList.remove("hidden");
                } else {
                    formCont.classList.remove("hidden");
                    waitCont.classList.add("hidden");
                }
                return;
            }
            
            if (state.status === "waiting") {
                renderLobby(state);
            } else if (state.status === "playing") {
                renderGame(state);
            } else if (state.status === "game_over") {
                renderGame(state);
                renderGameOver(state);
            }
            
            if (prevState) {
                if (state.current_round_cards.length > prevState.current_round_cards.length) {
                    SoundFX.playCardPlace();
                }
                if (state.deck_count < prevState.deck_count) {
                    SoundFX.playCardPlace();
                }
                if (state.deck_count > prevState.deck_count && state.discard_count < prevState.discard_count) {
                    SoundFX.playShuffle();
                }
            }
            
            prevState = state;
        }

        async function pollState() {
            // Polling tetap aktif bagi user anonym (tanpa playerId) agar bisa mendeteksi state permainan (waiting/playing)
            const url = playerId ? `/api/status?player_id=${playerId}` : "/api/status";
            const state = await apiGet(url);
            if (state) {
                renderState(state);
            }
        }

        function startPolling() {
            if (pollInterval) clearInterval(pollInterval);
            pollState();
            pollInterval = setInterval(pollState, 1000);
        }

        // Jalankan polling secara global sejak awal muat halaman
        startPolling();
    </script>
</body>
</html>
"""

# ==========================================
# SERVER STARTER ENGINE
# ==========================================
def run_server():
    port = PORT
    local_ip = get_local_ip()
    server_address = ('', port)
    
    try:
        httpd = ThreadedHTTPServer(server_address, GameRequestHandler)
        print("=================================================================")
        print("🃏 LOCAL MULTIPLAYER SERVER GAME KARTU CANGKULAN SELESAI DIBUAT 🃏")
        print("=================================================================")
        print(f"👉 Buka di browser Komputer Anda:  http://localhost:{port}")
        print(f"👉 Buka di browser HP Android Anda: http://{local_ip}:{port}")
        print("-----------------------------------------------------------------")
        print("💡 Catatan Jaringan Jarak Jauh:")
        print("   Pastikan HP Android Anda dan Komputer Server terhubung")
        print(f"   pada satu jaringan Wi-Fi yang SAMA agar HP bisa mengakses IP {local_ip}!")
        print("=================================================================")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        sys.exit(0) # Perbaikan 1: Berhasil menutup proses secara bersih tanpa NameError

if __name__ == '__main__':
    run_server()

"""Lance EffiSmart en mode local 100 % Python (sans Docker, sans PostgreSQL, sans Node.js).

Usage : python lancer.py   (ou double-clic sur « Lancer-EffiSmart.bat » sous Windows)

1. crée un environnement virtuel `.venv` au premier lancement ;
2. installe / met à jour les dépendances si besoin ;
3. démarre l'interface Streamlit et ouvre le navigateur dès qu'elle répond.

Si EffiSmart tourne déjà, le navigateur est simplement ouvert dessus ; si le port
est pris par une autre application, un port libre est choisi.

Les données sont stockées dans backend/data/ (base SQLite + exports).
Supprimer ce dossier réinitialise la démonstration.
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = [BACKEND / "requirements-ui.txt", BACKEND / "requirements.txt"]
STAMP = VENV / ".effismart-deps"
DEFAULT_PORT = 8501
API_PORT = 8000
STARTUP_TIMEOUT_S = 300


def say(message: str) -> None:
    print(message, flush=True)


def ensure_environment() -> None:
    if not VENV_PYTHON.exists():
        say("Création de l'environnement Python (.venv)…")
        venv.create(VENV, with_pip=True)
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in REQUIREMENTS)).hexdigest()
    if not STAMP.exists() or STAMP.read_text() != digest:
        say("Installation des dépendances (quelques minutes la première fois)…")
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS[0])], check=True)
        STAMP.write_text(digest)


def is_effismart_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def open_browser_when_ready(port: int) -> None:
    """Ouvre le navigateur dès que le serveur répond (le premier lancement peut être long)."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_effismart_running(port):
            webbrowser.open(f"http://localhost:{port}")
            return
        time.sleep(1)
    say("Le serveur ne répond pas : consultez les messages ci-dessus.")


def main() -> int:
    if sys.version_info < (3, 11):
        say("Python 3.11 ou plus récent est requis.")
        return 1
    ensure_environment()

    port = DEFAULT_PORT
    if not is_port_free(port):
        if is_effismart_running(port):
            say(f"EffiSmart tourne déjà : ouverture de http://localhost:{port}")
            webbrowser.open(f"http://localhost:{port}")
            return 0
        port = next(p for p in range(DEFAULT_PORT + 1, DEFAULT_PORT + 50) if is_port_free(p))
        say(f"Le port {DEFAULT_PORT} est occupé par une autre application : utilisation du port {port}.")

    say(f"EffiSmart démarre sur http://localhost:{port} — laissez cette fenêtre ouverte (Ctrl+C pour arrêter).")
    say("Premier lancement : génération des données de démonstration, comptez 1 à 2 minutes.")

    # API partenaires (même base locale), si le port 8000 est libre.
    api = None
    if is_port_free(API_PORT):
        api = subprocess.Popen([str(VENV_PYTHON), "-m", "app.local_api", str(API_PORT)], cwd=BACKEND)
        say(f"API partenaires : http://localhost:{API_PORT}/api/v1 (documentation : http://localhost:{API_PORT}/docs)")
    else:
        say(f"Port {API_PORT} occupé : l'API partenaires n'est pas démarrée.")

    threading.Thread(target=open_browser_when_ready, args=(port,), daemon=True).start()
    command = [
        str(VENV_PYTHON), "-m", "streamlit", "run", "effismart_ui.py",
        "--server.port", str(port), "--server.address", "127.0.0.1", "--server.headless", "true",
    ]
    try:
        return subprocess.run(command, cwd=BACKEND).returncode
    except KeyboardInterrupt:
        return 0
    finally:
        if api is not None:
            api.terminate()


if __name__ == "__main__":
    sys.exit(main())

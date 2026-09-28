"""Serveur de l'API EffiSmart en mode local (même base SQLite que l'interface Streamlit).

Lancé automatiquement par `lancer.py` sur http://127.0.0.1:8000 :
- API partenaires : /api/v1/… (clé d'API créée dans la page « Intégrations ») ;
- documentation interactive : /docs.
Le planificateur reste désactivé : l'interface se charge du rattrapage et de l'envoi des webhooks.
"""
from __future__ import annotations

import sys

from app.local import configure_local_environment


def main(port: int = 8000) -> None:
    configure_local_environment()
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)

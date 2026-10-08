"""Vocabulaire produit : la donnée la plus fraîche est celle de la veille.

Les gestionnaires de réseau publient la courbe de charge électrique de la veille chaque jour entre 12 h et 16 h,
et les consommations de gaz à J+1 ou J+2. EffiSmart parle donc de « suivi quotidien », de « données J+1 », de
« détection quotidienne des dérives » et d'« analyse de la courbe de charge au pas 30 min ». Un suivi à la seconde
suppose des capteurs sur site (IoT) : ce n'est pas ce que promet la V1, et le pas de 30 min suffit à tout ce qu'elle
promet. Parler de « suivi J+1 sur données réseau certifiées » est plus crédible face à un jury technique.

`tests/test_vocabulary.py` vérifie qu'aucun texte de l'application ne promet davantage.
"""
from app.models import Fluid

TAGLINE = "Suivi J+1 sur données réseau certifiées"
FRESHNESS = ("Données J+1 : la courbe de charge électrique de la veille est publiée chaque jour entre 12 h et 16 h, "
             "les consommations de gaz à J+1 ou J+2.")
# Jours de décalage maximal entre une journée et la publication de ses données (gaz : J+1 ou J+2).
PUBLICATION_LAG_DAYS = {Fluid.ELEC: 1, Fluid.GAS: 2}
ELEC_PUBLICATION_END_HOUR = 16  # courbe de la veille publiée au plus tard vers 16 h

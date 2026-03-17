# ============================================================================
#  Cluster Manager - Configuration
#  Copyright (c) 2026 Ayi NEDJIMI Consultants - Tous droits reserves
# ============================================================================
#
#  INSTRUCTIONS :
#  1. Copiez ce fichier : cp config.example.py config.py
#  2. Editez config.py avec vos informations de connexion Proxmox
#  3. Lancez l'application : python app.py
#
#  L'interface sera accessible sur http://localhost:5000
# ============================================================================


# ---------------------------------------------------------------------------
#  CONNEXION AU CLUSTER PROXMOX
# ---------------------------------------------------------------------------
#  hosts : Liste des IPs des noeuds du cluster (pour la redondance).
#          L'application essaiera chaque hote dans l'ordre jusqu'a trouver
#          un noeud qui repond. Un seul suffit pour monitorer tout le cluster.
#
#  port  : Port de l'API Proxmox (8006 par defaut)
#
#  username : Compte Proxmox au format user@realm (ex: root@pam, admin@pve)
#             Recommandation : creer un compte dedie avec le role PVEAuditor
#
#  password : Mot de passe du compte.
# ---------------------------------------------------------------------------
PROXMOX_CLUSTER = {
    "hosts": ["192.168.1.10", "192.168.1.11", "192.168.1.12"],
    "port": 8006,
    "username": "root@pam",
    "password": "YOUR_PASSWORD_HERE",
}


# ---------------------------------------------------------------------------
#  RAFRAICHISSEMENT AUTOMATIQUE
# ---------------------------------------------------------------------------
#  Intervalle en secondes entre chaque mise a jour automatique.
#  Recommande : 10 a 30 secondes. Trop bas = surcharge API Proxmox.
# ---------------------------------------------------------------------------
REFRESH_INTERVAL = 10


# ---------------------------------------------------------------------------
#  JOURNAUX (ONGLET JOURNAUX)
# ---------------------------------------------------------------------------
SYSLOG_LINES = 100       # Lignes de syslog par noeud (max 500)
TASK_LIMIT = 30           # Taches recentes par noeud
CLUSTER_LOG_MAX = 50      # Entrees du journal cluster


# ---------------------------------------------------------------------------
#  SERVEUR WEB
# ---------------------------------------------------------------------------
#  host  : "0.0.0.0" = ecoute sur toutes les interfaces (accessible reseau)
#           "127.0.0.1" = local uniquement
#  port  : Port HTTP (5000 par defaut)
#  debug : True = dev (auto-reload), False = production
# ---------------------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = 5000
WEB_DEBUG = False

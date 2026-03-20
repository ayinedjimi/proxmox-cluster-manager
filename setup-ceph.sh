#!/bin/bash
# ============================================================================
#  Ceph Cluster Setup for Proxmox VE 9
#  Version 3.0 - Procédure testée et validée
#  Compatible PVE 8.x / 9.x + Ceph Squid/Reef
#  Copyright (c) 2026 Ayi NEDJIMI Consultants
# ============================================================================

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DEBIAN_FRONTEND=noninteractive

# ── Pre-checks ──
[ "$(id -u)" -ne 0 ] && echo "ERREUR: root requis" && exit 1
command -v pveceph &>/dev/null || { echo "ERREUR: pas un noeud Proxmox"; exit 1; }
[ ! -t 0 ] && echo "ERREUR: terminal interactif requis" && exit 1
apt-get install -y -qq whiptail sgdisk &>/dev/null

T="Ceph Setup v3 - Ayi NEDJIMI Consultants"
BT="Proxmox VE Ceph"
HN=$(hostname)
MY_IP=$(ip -4 addr show vmbr0 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)
[ -z "$MY_IP" ] && MY_IP=$(ip -4 addr show scope global | grep -oP 'inet \K[\d.]+' | head -1)
[ -z "$MY_IP" ] && { whiptail --title "$T" --msgbox "ERREUR: IP introuvable" 8 40; exit 1; }

# ── Welcome ──
whiptail --title "$T" --backtitle "$BT" --msgbox \
"Setup Ceph pour Proxmox VE

Composants Ceph :
  MON = Monitor (etat du cluster)
  MGR = Manager (metriques)
  OSD = Stockage (1 disque = 1 OSD)
  Pool = Espace logique pour VMs

Noeud : $HN ($MY_IP)

Procedure :
  1. Lancer ce script sur le PREMIER noeud
  2. Puis sur chaque noeud secondaire

(c) 2026 Ayi NEDJIMI Consultants" 22 55

# ── Role ──
ROLE=$(whiptail --title "$T" --backtitle "$BT" --menu \
"Role de ce noeud :" 14 55 4 \
    "first" "Premier noeud (init + tout)" \
    "join"  "Rejoindre le cluster" \
    "osd"   "OSD uniquement" \
    "purge" "PURGER tout Ceph" \
    3>&1 1>&2 2>&3)
[ -z "$ROLE" ] && exit 0

# ============================================================================
#  PURGE
# ============================================================================
if [ "$ROLE" = "purge" ]; then
    whiptail --title "$T" --backtitle "$BT" --yesno "PURGE COMPLETE - IRREVERSIBLE !\n\nConfirmer ?" 10 45
    [ $? -ne 0 ] && exit 0
    whiptail --title "$T" --backtitle "$BT" --infobox "Purge en cours..." 6 35
    systemctl stop ceph.target &>/dev/null; sleep 2
    killall -9 ceph-mon ceph-mgr ceph-osd &>/dev/null; sleep 1
    umount /var/lib/ceph/osd/* &>/dev/null
    for dm in /dev/mapper/ceph-*; do [ -b "$dm" ] && dmsetup remove "$dm" &>/dev/null; done
    dmsetup remove_all &>/dev/null
    for vg in $(vgs --noheadings -o vg_name 2>/dev/null | grep ceph); do vgremove -ff "$vg" &>/dev/null; done
    for DISK in /dev/sd? /dev/vd? /dev/nvme[0-9]*n[0-9]*; do
        [ -b "$DISK" ] || continue
        HM=0; for P in ${DISK}*; do [ "$P" = "$DISK" ] && continue; MP=$(lsblk -n -o MOUNTPOINT "$P" 2>/dev/null | head -1); [ -n "$MP" ] && HM=1 && break; done
        [ "$HM" -eq 0 ] && { pvremove -ff "$DISK" &>/dev/null; wipefs -af "$DISK" &>/dev/null; sgdisk --zap-all "$DISK" &>/dev/null; dd if=/dev/zero of="$DISK" bs=1M count=200 &>/dev/null; }
    done
    rm -rf /var/lib/ceph /tmp/ceph* /tmp/monmap* &>/dev/null
    rm -f /etc/pve/ceph.conf /etc/pve/priv/ceph.* /etc/ceph/ceph.client.admin.keyring &>/dev/null
    rm -rf /etc/pve/ceph/ /etc/systemd/system/ceph-mon@.service.d /etc/systemd/system/ceph-osd@.service.d /etc/systemd/system/ceph-mgr@.service.d &>/dev/null
    apt-get purge -y ceph-mon ceph-mgr ceph-osd ceph-base ceph-volume ceph-mds &>/dev/null
    apt-get autoremove -y &>/dev/null; apt-get install -y ceph-common ceph-fuse &>/dev/null
    partprobe &>/dev/null; udevadm settle &>/dev/null; systemctl daemon-reload &>/dev/null
    whiptail --title "$T" --backtitle "$BT" --msgbox "Purge terminee sur $HN !" 8 40
    exit 0
fi

# ============================================================================
#  ETAPE 0 : FIX PERMISSIONS (bug connu PVE 9)
# ============================================================================
whiptail --title "$T" --backtitle "$BT" --msgbox \
"ETAPE 0 - FIX PERMISSIONS

Bug connu PVE 9 : /etc/pve/ceph.conf est
en root:www-data 640. Le user ceph ne peut
pas le lire -> services Ceph crashent.

Fix : overrides systemd pour lancer les
services Ceph en ceph:www-data." 14 55

whiptail --title "$T" --backtitle "$BT" --infobox "Application du fix permissions..." 6 45

usermod -aG www-data ceph 2>/dev/null

mkdir -p /etc/systemd/system/ceph-mon@.service.d
cat > /etc/systemd/system/ceph-mon@.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/ceph-mon -f --cluster ${CLUSTER} --id %i --setuser ceph --setgroup www-data
EOF

mkdir -p /etc/systemd/system/ceph-osd@.service.d
cat > /etc/systemd/system/ceph-osd@.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/ceph-osd -f --cluster ${CLUSTER} --id %i --setuser ceph --setgroup www-data
EOF

mkdir -p /etc/systemd/system/ceph-mgr@.service.d
cat > /etc/systemd/system/ceph-mgr@.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/ceph-mgr -f --cluster ${CLUSTER} --id %i --setuser ceph --setgroup www-data
EOF

systemctl daemon-reload

# Copier le keyring dans /etc/ceph/ en lisible (si deja init)
if [ -f /etc/pve/priv/ceph.client.admin.keyring ]; then
    cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring 2>/dev/null
    chmod 644 /etc/ceph/ceph.client.admin.keyring 2>/dev/null
fi

# ============================================================================
#  ETAPE 1 : INSTALLATION
# ============================================================================
whiptail --title "$T" --backtitle "$BT" --msgbox \
"ETAPE 1 - INSTALLATION DE CEPH

Paquets : ceph-mon, ceph-mgr, ceph-osd
Source : depot Proxmox no-subscription
Duree : 2-3 minutes" 12 50

if command -v ceph-mon &>/dev/null; then
    whiptail --title "$T" --backtitle "$BT" --msgbox "Ceph deja installe." 8 35
else
    whiptail --title "$T" --backtitle "$BT" --infobox "Installation Ceph (2-3 min)..." 6 45
    pveceph install --repository no-subscription </dev/null &>/dev/null
    if ! command -v ceph-mon &>/dev/null; then
        apt-get update -qq &>/dev/null
        apt-get install -y --allow-downgrades ceph-mon ceph-mgr ceph-osd ceph-volume &>/dev/null
    fi
    if command -v ceph-mon &>/dev/null; then
        whiptail --title "$T" --backtitle "$BT" --msgbox "Ceph installe !" 8 30
    else
        whiptail --title "$T" --backtitle "$BT" --msgbox "ERREUR installation" 8 35
        exit 1
    fi
fi

mkdir -p /var/lib/ceph/{mon,mgr,osd,tmp,crash,bootstrap-osd,bootstrap-mgr}
chown -R ceph:ceph /var/lib/ceph/

# ============================================================================
#  PREMIER NOEUD : INIT + MON + MGR + OSD + POOL
# ============================================================================
if [ "$ROLE" = "first" ]; then

    # ── Reseau ──
    DEFAULT_NET=$(ip -4 route show | grep -v default | head -1 | awk '{print $1}')
    [ -z "$DEFAULT_NET" ] && DEFAULT_NET="$(echo "$MY_IP" | sed 's/\.[0-9]*$/.0/')/24"
    PUBLIC_NET=$(whiptail --title "$T" --backtitle "$BT" --inputbox "Reseau public Ceph (CIDR) :" 10 50 "$DEFAULT_NET" 3>&1 1>&2 2>&3)
    [ -z "$PUBLIC_NET" ] && PUBLIC_NET="$DEFAULT_NET"

    # ── Init ──
    if [ -f /etc/pve/ceph.conf ]; then
        whiptail --title "$T" --backtitle "$BT" --yesno "Ceph deja init. Reinitialiser ?" 8 45
        [ $? -eq 0 ] && { rm -f /etc/pve/ceph.conf /etc/pve/priv/ceph.* &>/dev/null; rm -rf /etc/pve/ceph/ &>/dev/null; rm -rf /var/lib/ceph/mon/* /var/lib/ceph/mgr/* /var/lib/ceph/osd/* &>/dev/null; }
    fi
    if [ ! -f /etc/pve/ceph.conf ]; then
        whiptail --title "$T" --backtitle "$BT" --infobox "pveceph init..." 6 35
        pveceph init --network "$PUBLIC_NET" &>/dev/null
        [ ! -f /etc/pve/ceph.conf ] && { whiptail --title "$T" --backtitle "$BT" --msgbox "ERREUR: init echoue" 8 35; exit 1; }
    fi

    # ── Bootstrap MON (manuel - pveceph mon create deadlock sur 1er noeud) ──
    whiptail --title "$T" --backtitle "$BT" --msgbox \
"ETAPE 3 - BOOTSTRAP PREMIER MON

Le premier MON necessite un bootstrap
manuel car pveceph mon create deadlock.

monmaptool + ceph-mon --mkfs
Puis systemctl start (avec retry)" 14 50

    whiptail --title "$T" --backtitle "$BT" --infobox "Bootstrap MON (30-60 sec)..." 6 45

    FSID=$(python3 -c "
import re
c = open('/etc/pve/ceph.conf').read()
m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', c)
print(m.group(0) if m else '')
")
    [ -z "$FSID" ] && { whiptail --title "$T" --backtitle "$BT" --msgbox "ERREUR: FSID vide" 8 35; exit 1; }

    rm -rf /var/lib/ceph/mon/ceph-$HN; mkdir -p /var/lib/ceph/mon/ceph-$HN
    rm -f /tmp/ceph.mon.keyring /tmp/monmap

    ceph-authtool --create-keyring /tmp/ceph.mon.keyring --gen-key -n mon. --cap mon 'allow *' &>/dev/null
    ceph-authtool /tmp/ceph.mon.keyring --import-keyring /etc/pve/priv/ceph.client.admin.keyring &>/dev/null
    chmod 600 /tmp/ceph.mon.keyring

    monmaptool --create --add "$HN" "$MY_IP" --fsid "$FSID" /tmp/monmap &>/dev/null
    [ ! -f /tmp/monmap ] && { whiptail --title "$T" --backtitle "$BT" --msgbox "ERREUR: monmaptool echoue" 8 40; exit 1; }

    ceph-mon --mkfs -i "$HN" --monmap /tmp/monmap --keyring /tmp/ceph.mon.keyring &>/dev/null 2>&1
    chown -R ceph:www-data /var/lib/ceph/mon/ceph-$HN

    ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf
    cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring 2>/dev/null

    # Fix ceph.conf (ajout mon_host)
    python3 -c "
import re
c = open('/etc/pve/ceph.conf').read()
c = re.sub(r'^tmon_host.*\n', '', c, flags=re.MULTILINE)
if 'mon_host' not in c:
    c = c.replace('[global]', '[global]\n\tmon_host = $MY_IP')
if 'mon.$HN' not in c:
    c += '\n[mon.$HN]\n\tpublic_addr = $MY_IP\n'
open('/etc/pve/ceph.conf', 'w').write(c)
"

    # Start MON (retry pattern)
    systemctl enable ceph-mon@$HN &>/dev/null
    for i in 1 2 3 4; do
        systemctl reset-failed ceph-mon@$HN &>/dev/null
        systemctl start ceph-mon@$HN &>/dev/null
        sleep 15
        systemctl is-active ceph-mon@$HN &>/dev/null && break
    done

    if systemctl is-active ceph-mon@$HN &>/dev/null; then
        sleep 5
        ceph config set mon auth_allow_insecure_global_id_reclaim false &>/dev/null
        ceph mon enable-msgr2 &>/dev/null
        whiptail --title "$T" --backtitle "$BT" --msgbox "MON actif !\n\n$(timeout 10 ceph -s 2>&1 | head -8)" 16 60
    else
        whiptail --title "$T" --backtitle "$BT" --msgbox "MON echoue.\n\nsystemctl reset-failed ceph-mon@$HN\nsystemctl start ceph-mon@$HN" 12 55
        exit 1
    fi

    # ── MGR ──
    whiptail --title "$T" --backtitle "$BT" --infobox "Creation MGR..." 6 35
    pveceph mgr create &>/dev/null 2>&1
    sleep 10
    if ! systemctl is-active ceph-mgr@$HN &>/dev/null; then
        systemctl reset-failed ceph-mgr@$HN &>/dev/null; systemctl start ceph-mgr@$HN &>/dev/null
        sleep 10
    fi

    # Bootstrap-osd keyring
    ceph auth get client.bootstrap-osd > /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null
    chown ceph:ceph /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null

fi

# ============================================================================
#  NOEUD SECONDAIRE : MON + MGR
# ============================================================================
if [ "$ROLE" = "join" ]; then

    whiptail --title "$T" --backtitle "$BT" --infobox "Preparation..." 6 35
    ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf
    cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring 2>/dev/null
    chmod 644 /etc/ceph/ceph.client.admin.keyring 2>/dev/null
    # Regenerer le keyring proprement depuis le cluster (si accessible)
    ceph auth get-or-create client.admin mon 'allow *' osd 'allow *' mds 'allow *' mgr 'allow *' > /etc/ceph/ceph.client.admin.keyring 2>/dev/null
    chmod 644 /etc/ceph/ceph.client.admin.keyring 2>/dev/null

    if ! timeout 15 ceph -s &>/dev/null; then
        whiptail --title "$T" --backtitle "$BT" --msgbox "ERREUR: Cluster inaccessible.\nLe premier noeud doit avoir un MON actif." 10 50
        exit 1
    fi

    # MON (bootstrap via monmap du cluster)
    whiptail --title "$T" --backtitle "$BT" --infobox "Bootstrap MON sur $HN (60 sec)..." 6 50
    systemctl stop ceph-mon@$HN &>/dev/null
    rm -rf /var/lib/ceph/mon/ceph-$HN
    mkdir -p /var/lib/ceph/mon/ceph-$HN

    # Recuperer monmap du cluster
    GETMAP_OUT=$(ceph mon getmap -o /tmp/monmap.bin 2>&1)
    if [ ! -f /tmp/monmap.bin ]; then
        whiptail --title "$T" --backtitle "$BT" --msgbox "Echec ceph mon getmap.\n\n$GETMAP_OUT\n\nVerifiez que le cluster est actif sur le premier noeud." 14 55
    else
        # Creer un keyring MON avec les bons caps
        ceph auth get mon. -o /tmp/ceph.mon.keyring 2>/dev/null
        if [ ! -f /tmp/ceph.mon.keyring ]; then
            # Fallback : creer manuellement
            ceph-authtool --create-keyring /tmp/ceph.mon.keyring --gen-key -n mon. --cap mon 'allow *' 2>/dev/null
        fi
        # Importer le keyring admin dans le keyring MON
        ceph-authtool /tmp/ceph.mon.keyring --import-keyring /etc/ceph/ceph.client.admin.keyring 2>/dev/null
        chmod 600 /tmp/ceph.mon.keyring

        # mkfs du MON avec le bon keyring
        MKFS_OUT=$(ceph-mon --mkfs -i "$HN" --monmap /tmp/monmap.bin --keyring /tmp/ceph.mon.keyring 2>&1)
        chown -R ceph:www-data /var/lib/ceph/mon/ceph-$HN

        # Verifier que le datadir n'est pas vide
        if [ ! -f /var/lib/ceph/mon/ceph-$HN/kv_backend ]; then
            whiptail --title "$T" --backtitle "$BT" --msgbox "mkfs MON echoue.\n\n$MKFS_OUT" 12 55
        else
            # Demarrer avec retry (attente longue pour nested)
            systemctl enable ceph-mon@$HN &>/dev/null
            for i in 1 2 3 4; do
                systemctl reset-failed ceph-mon@$HN &>/dev/null
                systemctl start ceph-mon@$HN &>/dev/null
                sleep 20
                systemctl is-active ceph-mon@$HN &>/dev/null && break
            done
        fi
    fi

    if systemctl is-active ceph-mon@$HN &>/dev/null; then
        whiptail --title "$T" --backtitle "$BT" --msgbox "MON actif sur $HN !" 8 35
    else
        whiptail --title "$T" --backtitle "$BT" --msgbox "MON pas actif.\nLe cluster fonctionne quand meme.\nOn continue avec MGR + OSD." 10 50
    fi

    # MGR
    whiptail --title "$T" --backtitle "$BT" --infobox "Creation MGR..." 6 35
    rm -rf /var/lib/ceph/mgr/ceph-$HN &>/dev/null
    pveceph mgr create &>/dev/null 2>&1
    sleep 10
    if ! systemctl is-active ceph-mgr@$HN &>/dev/null; then
        systemctl reset-failed ceph-mgr@$HN &>/dev/null; systemctl start ceph-mgr@$HN &>/dev/null
        sleep 5
    fi

    # Copy bootstrap-osd keyring if missing
    if [ ! -f /var/lib/ceph/bootstrap-osd/ceph.keyring ]; then
        ceph auth get client.bootstrap-osd > /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null
        chown ceph:ceph /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null
    fi
fi

# ============================================================================
#  OSD (tous les roles sauf purge)
# ============================================================================
if [ "$ROLE" != "purge" ]; then

    whiptail --title "$T" --backtitle "$BT" --msgbox \
"ETAPE OSD - STOCKAGE

1 disque = 1 OSD (disque ENTIER dedie)
Ne JAMAIS utiliser le disque systeme
Backend : Bluestore

Methode : prepare + activate (pas create)
pour eviter le rollback automatique." 14 50

    DISK_MENU=()
    DISK_IDX=0
    while IFS= read -r line; do
        DISK=$(echo "$line" | awk '{print $1}')
        SIZE=$(echo "$line" | awk '{print $2}')
        [ -b "$DISK" ] || continue
        HM=0; for P in ${DISK}*; do [ "$P" = "$DISK" ] && continue; MP=$(lsblk -n -o MOUNTPOINT "$P" 2>/dev/null | head -1); [ -n "$MP" ] && HM=1 && break; done
        [ "$HM" -eq 1 ] && continue
        NPARTS=$(lsblk -n -o TYPE "$DISK" 2>/dev/null | grep -c part)
        if [ "$NPARTS" -eq 0 ]; then
            ROTA=$(lsblk -dn -o ROTA "$DISK" 2>/dev/null | tr -d ' ')
            TL="HDD"; [ "$ROTA" = "0" ] && TL="SSD"
            DISK_MENU+=("$DISK" "[$SIZE] $TL" "OFF")
            DISK_IDX=$((DISK_IDX + 1))
        fi
    done < <(lsblk -dn -o NAME,SIZE -p 2>/dev/null | grep -vE "loop|sr|ram")

    if [ "$DISK_IDX" -eq 0 ]; then
        whiptail --title "$T" --backtitle "$BT" --msgbox "Aucun disque dispo pour OSD." 8 45
    else
        SELECTED=$(whiptail --title "$T" --backtitle "$BT" \
            --checklist "Disques pour OSD :" 16 50 "$DISK_IDX" \
            "${DISK_MENU[@]}" 3>&1 1>&2 2>&3)

        if [ -n "$SELECTED" ]; then
            whiptail --title "$T" --backtitle "$BT" --yesno "CREER OSD sur :\n$SELECTED\n\nDonnees PERDUES !" 10 45
            if [ $? -eq 0 ]; then
                for DISK in $(echo "$SELECTED" | tr -d '"'); do
                    whiptail --title "$T" --backtitle "$BT" --infobox "OSD: $DISK\n\nWipe + prepare + activate (~2 min)" 8 50

                    # Wipe agressif
                    dmsetup remove_all &>/dev/null
                    for vg in $(pvs --noheadings -o vg_name "$DISK" 2>/dev/null); do vgremove -ff "$vg" &>/dev/null; done
                    pvremove -ff "$DISK" &>/dev/null
                    wipefs -af "$DISK" &>/dev/null
                    sgdisk --zap-all "$DISK" &>/dev/null
                    dd if=/dev/zero of="$DISK" bs=1M count=500 &>/dev/null 2>&1
                    partprobe "$DISK" &>/dev/null; udevadm settle &>/dev/null
                    sleep 5

                    # Prepare (sans start - evite rollback)
                    PREP_OUT=$(ceph-volume lvm prepare --data "$DISK" 2>&1)
                    PREP_RET=$?
                    if [ $PREP_RET -ne 0 ]; then
                        whiptail --title "$T" --backtitle "$BT" --msgbox "Prepare echoue sur $DISK\n\n$(echo "$PREP_OUT" | tail -5)" 14 55
                        continue
                    fi

                    # Attendre que le prepare soit termine
                    sleep 5

                    # Activate (peut echouer au premier start, c'est normal)
                    ceph-volume lvm activate --all 2>/dev/null
                    sleep 8

                    # Trouver l'OSD ID
                    OSD_ID=$(ls /var/lib/ceph/osd/ 2>/dev/null | tail -1 | sed 's/ceph-//')
                    if [ -z "$OSD_ID" ]; then
                        whiptail --title "$T" --backtitle "$BT" --msgbox "OSD non trouve apres prepare.\nVerifiez: ceph-volume lvm list" 10 50
                        continue
                    fi

                    # Start avec retry (le premier start echoue toujours en nested)
                    for i in 1 2 3 4; do
                        systemctl reset-failed ceph-osd@$OSD_ID 2>/dev/null
                        systemctl start ceph-osd@$OSD_ID 2>/dev/null
                        sleep 10
                        systemctl is-active ceph-osd@$OSD_ID &>/dev/null && break
                    done

                    if systemctl is-active ceph-osd@$OSD_ID &>/dev/null; then
                        whiptail --title "$T" --backtitle "$BT" --msgbox "OSD.$OSD_ID actif sur $DISK !" 8 40
                    else
                        whiptail --title "$T" --backtitle "$BT" --msgbox "OSD.$OSD_ID echoue.\n\nEssayez manuellement:\n  systemctl reset-failed ceph-osd@$OSD_ID\n  systemctl start ceph-osd@$OSD_ID" 12 55
                    fi
                done
            fi
        fi
    fi
fi

# ============================================================================
#  POOL (premier noeud uniquement)
# ============================================================================
if [ "$ROLE" = "first" ]; then
    sleep 10
    OSD_COUNT=$(timeout 10 ceph osd stat 2>/dev/null | grep -oP '\d+ osds' | head -1 | awk '{print $1}')
    OSD_COUNT=${OSD_COUNT:-0}

    if [ "$OSD_COUNT" -gt 0 ]; then
        POOL_NAME=$(whiptail --title "$T" --backtitle "$BT" --inputbox "Nom du pool :" 10 45 "ceph-vm" 3>&1 1>&2 2>&3)
        POOL_NAME=$(echo "${POOL_NAME:-ceph-vm}" | tr -cd 'a-zA-Z0-9_-')
        [ -z "$POOL_NAME" ] && POOL_NAME="ceph-vm"

        if timeout 10 ceph osd pool ls 2>/dev/null | grep -q "^${POOL_NAME}$"; then
            whiptail --title "$T" --backtitle "$BT" --msgbox "Pool '$POOL_NAME' existe deja." 8 40
        else
            whiptail --title "$T" --backtitle "$BT" --infobox "Creation pool $POOL_NAME..." 6 40
            ceph osd pool create "$POOL_NAME" 128 replicated &>/dev/null
            ceph osd pool set "$POOL_NAME" size 3 &>/dev/null
            ceph osd pool set "$POOL_NAME" min_size 2 &>/dev/null
            ceph osd pool application enable "$POOL_NAME" rbd &>/dev/null
            # Fix keyring: copier dans /etc/ceph/ en lisible (bug PVE 9 permissions /etc/pve/)
            ceph auth get-or-create client.admin mon 'allow *' osd 'allow *' mds 'allow *' mgr 'allow *' > /etc/ceph/ceph.client.admin.keyring 2>/dev/null
            chmod 644 /etc/ceph/ceph.client.admin.keyring
            pvesm add rbd "$POOL_NAME" -pool "$POOL_NAME" -content images,rootdir -krbd 0 -keyring /etc/ceph/ceph.client.admin.keyring &>/dev/null 2>&1
            whiptail --title "$T" --backtitle "$BT" --msgbox "Pool '$POOL_NAME' cree !" 8 40
        fi
    fi
fi

# ============================================================================
#  FINAL
# ============================================================================
HEALTH=$(timeout 10 ceph health 2>&1)
STATUS=$(timeout 10 ceph -s 2>&1 | head -12)

whiptail --title "$T - Termine !" --backtitle "$BT" --msgbox \
"CEPH CONFIGURE SUR $HN !

$STATUS

Prochaine etape :
  Si premier noeud -> lancer ce script
  sur les autres en mode 'Rejoindre'

(c) 2026 Ayi NEDJIMI Consultants" 26 60

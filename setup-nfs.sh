#!/bin/bash
# ============================================================================
#  NFS Server Setup Script for Proxmox Shared Storage
#  Interactive TUI with whiptail
#  Compatible Debian 12 / 13
#  Copyright (c) 2026 Ayi NEDJIMI Consultants
# ============================================================================

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DEBIAN_FRONTEND=noninteractive

# Check root
if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root"
    exit 1
fi

# ── Install prerequisites silently ──
apt-get update -qq > /dev/null 2>&1
apt-get install -y -qq whiptail parted nfs-kernel-server > /dev/null 2>&1

TITLE="NFS Setup - Ayi NEDJIMI Consultants"
BACKTITLE="Proxmox Shared Storage NFS Setup"

# ── Welcome ──
whiptail --title "$TITLE" --backtitle "$BACKTITLE" --msgbox \
"Bienvenue dans l'assistant de configuration NFS\npour stockage partage Proxmox VE.\n\nCe script va :\n  1. Detecter les disques disponibles\n  2. Partitionner et formater le volume choisi\n  3. Installer et configurer le serveur NFS\n  4. Afficher les parametres pour Proxmox\n\n(c) 2026 Ayi NEDJIMI Consultants" 18 60

# ── Detect available volumes ──
MENU_ITEMS=()
IDX=0

# Whole disks without mounted partitions
for DISK in /dev/sd? /dev/vd? /dev/nvme?n?; do
    [ -b "$DISK" ] || continue
    # Check for mounted partitions (= system disk, skip)
    HAS_MOUNT=0
    for P in ${DISK}*; do
        [ "$P" = "$DISK" ] && continue
        MP=$(lsblk -n -o MOUNTPOINT "$P" 2>/dev/null | head -1)
        [ -n "$MP" ] && HAS_MOUNT=1 && break
    done
    NPARTS=$(lsblk -n -o TYPE "$DISK" 2>/dev/null | grep -c part)
    SIZE=$(lsblk -dn -o SIZE "$DISK" 2>/dev/null | tr -d ' ')
    if [ "$NPARTS" -eq 0 ]; then
        MENU_ITEMS+=("disk|$DISK" "$DISK  [$SIZE]  Disque entier vide (recommande)")
        IDX=$((IDX + 1))
    fi
done

# Unmounted partitions
for PART in $(lsblk -ln -o NAME,TYPE -p 2>/dev/null | awk '$2=="part"{print $1}'); do
    MP=$(lsblk -n -o MOUNTPOINT "$PART" 2>/dev/null | head -1)
    [ -n "$MP" ] && continue
    FS=$(lsblk -n -o FSTYPE "$PART" 2>/dev/null | head -1)
    [ "$FS" = "swap" ] && continue
    [ "$FS" = "vfat" ] && continue
    SIZE=$(lsblk -n -o SIZE "$PART" 2>/dev/null | head -1 | tr -d ' ')
    if [ -z "$FS" ]; then
        MENU_ITEMS+=("part|$PART|noformat" "$PART  [$SIZE]  Partition non formatee")
    else
        MENU_ITEMS+=("part|$PART|$FS" "$PART  [$SIZE]  Partition $FS non montee")
    fi
    IDX=$((IDX + 1))
done

if [ "$IDX" -eq 0 ]; then
    whiptail --title "$TITLE" --backtitle "$BACKTITLE" --msgbox \
    "ERREUR: Aucun disque ou partition disponible !\n\nTous les disques sont utilises par le systeme.\nAjoutez un nouveau disque et relancez le script." 12 55
    exit 1
fi

# ── Disk selection menu ──
SELECTED=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --menu "Choisissez le volume pour le stockage NFS :" 20 70 $IDX \
    "${MENU_ITEMS[@]}" 3>&1 1>&2 2>&3)

if [ -z "$SELECTED" ]; then
    echo "Annule."
    exit 0
fi

SEL_TYPE=$(echo "$SELECTED" | cut -d'|' -f1)
SEL_DEV=$(echo "$SELECTED" | cut -d'|' -f2)
SEL_FS=$(echo "$SELECTED" | cut -d'|' -f3)
SEL_SIZE=$(lsblk -dn -o SIZE "$SEL_DEV" 2>/dev/null || lsblk -n -o SIZE "$SEL_DEV" 2>/dev/null | head -1)

# ── NFS export path ──
NFS_PATH=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --inputbox "Chemin du repertoire NFS a exporter :" 10 60 \
    "/srv/nfs-proxmox" 3>&1 1>&2 2>&3)

[ -z "$NFS_PATH" ] && NFS_PATH="/srv/nfs-proxmox"

# ── Subnet ──
SUBNET=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --inputbox "Sous-reseau autorise a acceder au NFS :" 10 60 \
    "192.168.100.0/24" 3>&1 1>&2 2>&3)

[ -z "$SUBNET" ] && SUBNET="192.168.100.0/24"

# ── Storage name for Proxmox ──
STORAGE_ID=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --inputbox "Nom du stockage dans Proxmox (ID) :" 10 60 \
    "nfs-shared" 3>&1 1>&2 2>&3)

[ -z "$STORAGE_ID" ] && STORAGE_ID="nfs-shared"

# ── Content types ──
CONTENT=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --checklist "Types de contenu a stocker :" 16 60 5 \
    "images"   "Images disque VM (qcow2, raw)" ON \
    "iso"      "Images ISO" ON \
    "backup"   "Sauvegardes VZDump" ON \
    "vztmpl"   "Templates de containers" ON \
    "rootdir"  "Rootdir containers" OFF \
    3>&1 1>&2 2>&3)

[ -z "$CONTENT" ] && CONTENT='"images" "iso" "backup" "vztmpl"'
# Clean content for pvesm
PVE_CONTENT=$(echo "$CONTENT" | tr -d '"' | tr ' ' ',')

# ── Filesystem type ──
FSTYPE=$(whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
    --menu "Systeme de fichiers :" 14 60 3 \
    "ext4"  "ext4 - Standard, fiable (recommande)" \
    "xfs"   "xfs  - Haute performance, gros fichiers" \
    "skip"  "Ne pas formater (partition deja formatee)" \
    3>&1 1>&2 2>&3)

[ -z "$FSTYPE" ] && FSTYPE="ext4"

# ── Confirmation ──
MY_IP=$(ip -4 addr show 2>/dev/null | grep 'inet ' | grep -v '127.0.0' | awk '{print $2}' | cut -d/ -f1 | head -1)

whiptail --title "$TITLE" --backtitle "$BACKTITLE" --yesno \
"Resume de la configuration :\n\n\
  Volume        : $SEL_DEV ($SEL_SIZE)\n\
  Type          : $SEL_TYPE\n\
  Filesystem    : $FSTYPE\n\
  Montage       : $NFS_PATH\n\
  Subnet NFS    : $SUBNET\n\
  Proxmox ID    : $STORAGE_ID\n\
  Contenu       : $PVE_CONTENT\n\
  IP serveur    : $MY_IP\n\n\
ATTENTION: Le volume sera formate !\n\
Toutes les donnees seront perdues !\n\n\
Confirmer l'installation ?" 22 60

if [ $? -ne 0 ]; then
    echo "Annule par l'utilisateur."
    exit 0
fi

# ============================================================================
#  EXECUTION
# ============================================================================

{
    # ── Partition ──
    echo "XXX"
    echo 10
    echo "Partitionnement de $SEL_DEV..."
    echo "XXX"

    if [ "$SEL_TYPE" = "disk" ]; then
        parted -s "$SEL_DEV" mklabel gpt 2>/dev/null
        parted -s "$SEL_DEV" mkpart primary "$FSTYPE" 0% 100% 2>/dev/null
        sleep 2
        if [ -b "${SEL_DEV}1" ]; then
            NFS_DEVICE="${SEL_DEV}1"
        elif [ -b "${SEL_DEV}p1" ]; then
            NFS_DEVICE="${SEL_DEV}p1"
        fi
    else
        NFS_DEVICE="$SEL_DEV"
    fi

    # ── Format ──
    echo "XXX"
    echo 30
    echo "Formatage $NFS_DEVICE en $FSTYPE..."
    echo "XXX"

    if [ "$FSTYPE" = "ext4" ]; then
        mkfs.ext4 -q -F "$NFS_DEVICE" 2>/dev/null
    elif [ "$FSTYPE" = "xfs" ]; then
        apt-get install -y -qq xfsprogs > /dev/null 2>&1
        mkfs.xfs -f "$NFS_DEVICE" 2>/dev/null
    fi

    # ── Mount ──
    echo "XXX"
    echo 50
    echo "Montage sur $NFS_PATH..."
    echo "XXX"

    mkdir -p "$NFS_PATH"
    mountpoint -q "$NFS_PATH" 2>/dev/null && umount "$NFS_PATH"
    mount "$NFS_DEVICE" "$NFS_PATH"

    DEV_UUID=$(blkid -s UUID -o value "$NFS_DEVICE")
    grep -q "$DEV_UUID" /etc/fstab 2>/dev/null || \
        echo "UUID=$DEV_UUID $NFS_PATH $FSTYPE defaults,nofail 0 2" >> /etc/fstab

    mkdir -p "$NFS_PATH"/{images,backup,iso,template,rootdir}
    chmod -R 777 "$NFS_PATH"

    # ── Configure NFS ──
    echo "XXX"
    echo 70
    echo "Configuration de l'export NFS..."
    echo "XXX"

    sed -i "\|$NFS_PATH|d" /etc/exports 2>/dev/null
    echo "$NFS_PATH $SUBNET(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports
    exportfs -ra 2>/dev/null
    systemctl enable nfs-kernel-server > /dev/null 2>&1
    systemctl restart nfs-kernel-server 2>/dev/null

    # ── Firewall (if ufw active) ──
    echo "XXX"
    echo 85
    echo "Configuration firewall..."
    echo "XXX"

    if command -v ufw > /dev/null 2>&1 && ufw status | grep -q "active"; then
        ufw allow from "$SUBNET" to any port nfs > /dev/null 2>&1
    fi

    # ── Done ──
    echo "XXX"
    echo 100
    echo "Termine !"
    echo "XXX"

} | whiptail --title "$TITLE" --backtitle "$BACKTITLE" --gauge "Preparation..." 8 60 0

# ── Final summary ──
TOTAL=$(df -h "$NFS_PATH" 2>/dev/null | tail -1 | awk '{print $2}')
AVAIL=$(df -h "$NFS_PATH" 2>/dev/null | tail -1 | awk '{print $4}')
EXPORT_CHECK=$(exportfs -v 2>/dev/null | grep "$NFS_PATH" | head -1)

whiptail --title "$TITLE - Installation terminee !" --backtitle "$BACKTITLE" --msgbox \
"SERVEUR NFS CONFIGURE AVEC SUCCES !\n\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  Serveur NFS\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  IP         : $MY_IP\n\
  Export     : $NFS_PATH\n\
  Device     : $NFS_DEVICE\n\
  Filesystem : $FSTYPE\n\
  Taille     : $TOTAL\n\
  Disponible : $AVAIL\n\
  Subnet     : $SUBNET\n\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  Configuration Proxmox (GUI)\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  Datacenter > Storage > Add > NFS\n\n\
  ID       : $STORAGE_ID\n\
  Server   : $MY_IP\n\
  Export   : $NFS_PATH\n\
  Content  : $PVE_CONTENT\n\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  Configuration Proxmox (CLI)\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  pvesm add nfs $STORAGE_ID \\ \n\
    -server $MY_IP \\ \n\
    -export $NFS_PATH \\ \n\
    -path /mnt/pve/$STORAGE_ID \\ \n\
    -content $PVE_CONTENT\n\n\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
  Test : showmount -e $MY_IP" 38 60

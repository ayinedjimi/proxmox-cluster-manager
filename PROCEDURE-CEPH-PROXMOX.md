# Procédure Ceph sur Proxmox VE 9 - Mode Opératoire Fiable
## Testé et validé en environnement nested Hyper-V
### Copyright (c) 2026 Ayi NEDJIMI Consultants

---

## Prérequis

- Cluster Proxmox 3 noeuds minimum (Corosync OK, quorum OK)
- 1 disque vierge dédié par noeud pour les OSD (ex: /dev/sdc)
- Réseau fonctionnel entre tous les noeuds (latence < 1ms)
- NTP synchronisé (chrony)

## Architecture cible

```
proxmox1 (192.168.100.1) : MON + MGR + OSD
proxmox2 (192.168.100.2) : MON + MGR + OSD
proxmox3 (192.168.100.3) : MON + MGR + OSD
```

---

## ETAPE 0 : Fix permissions (OBLIGATOIRE, sur les 3 noeuds)

> ⚠️ Bug connu PVE 9 : /etc/pve/ceph.conf est en root:www-data 640.
> Le user ceph ne peut pas le lire. Les services Ceph crashent sans ce fix.

**Sur CHAQUE noeud (proxmox1, proxmox2, proxmox3) :**

```bash
# Ajouter ceph au groupe www-data
usermod -aG www-data ceph

# Override systemd : services Ceph tournent en ceph:www-data
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
```

---

## ETAPE 1 : Installation Ceph (sur les 3 noeuds)

**Sur CHAQUE noeud :**

```bash
pveceph install --repository no-subscription
```

Si ça bloque sur Y/n ou échoue :

```bash
apt-get update -qq
apt-get install -y --allow-downgrades ceph-mon ceph-mgr ceph-osd ceph-volume
```

Puis sur chaque noeud :

```bash
# Créer les répertoires nécessaires
mkdir -p /var/lib/ceph/{mon,mgr,osd,tmp,crash,bootstrap-osd,bootstrap-mgr}
chown -R ceph:ceph /var/lib/ceph/
```

**Vérification :**

```bash
ceph-mon --version
# Doit afficher : ceph version 19.x.x squid (stable)
```

---

## ETAPE 2 : Initialisation du cluster (proxmox1 UNIQUEMENT)

```bash
pveceph init --network 192.168.100.0/24
```

**Vérification :**

```bash
cat /etc/pve/ceph.conf | grep fsid
# Doit afficher un UUID
```

---

## ETAPE 3 : Bootstrap premier MON (proxmox1 UNIQUEMENT)

> ⚠️ pveceph mon create ne fonctionne PAS pour le premier MON.
> Il faut bootstrapper manuellement avec monmaptool.

```bash
# Récupérer le FSID
FSID=$(python3 -c "
import re
c = open('/etc/pve/ceph.conf').read()
m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', c)
print(m.group(0))
")
echo "FSID: $FSID"

# Créer le datadir
mkdir -p /var/lib/ceph/mon/ceph-$(hostname)

# Créer les keyrings
ceph-authtool --create-keyring /tmp/ceph.mon.keyring --gen-key -n mon. --cap mon 'allow *'
ceph-authtool /tmp/ceph.mon.keyring --import-keyring /etc/pve/priv/ceph.client.admin.keyring
chmod 600 /tmp/ceph.mon.keyring

# Créer le monmap
monmaptool --create --add $(hostname) $(hostname -I | awk '{print $1}') --fsid $FSID /tmp/monmap

# Bootstrap du MON
ceph-mon --mkfs -i $(hostname) --monmap /tmp/monmap --keyring /tmp/ceph.mon.keyring

# Permissions (www-data car override systemd)
chown -R ceph:www-data /var/lib/ceph/mon/ceph-$(hostname)

# Symlink config + keyring
ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf
cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring
```

**Ajouter mon_host dans la config (IMPORTANT) :**

```bash
python3 -c "
c = open('/etc/pve/ceph.conf').read()
if 'mon_host' not in c:
    c = c.replace('[global]', '[global]\n\tmon_host = $(hostname -I | awk '{print $1}')')
if 'mon.$(hostname)' not in c:
    c += '\n[mon.$(hostname)]\n\tpublic_addr = $(hostname -I | awk '{print $1}')\n'
open('/etc/pve/ceph.conf', 'w').write(c)
print('OK')
"
```

**Démarrer le MON (avec retry - le premier start échoue toujours) :**

```bash
systemctl enable ceph-mon@$(hostname)
systemctl start ceph-mon@$(hostname)

# Attendre 15 secondes
sleep 15

# Si pas actif, retry :
systemctl is-active ceph-mon@$(hostname) || {
    systemctl reset-failed ceph-mon@$(hostname)
    systemctl start ceph-mon@$(hostname)
    sleep 15
}
```

**Vérification (OBLIGATOIRE avant de continuer) :**

```bash
systemctl is-active ceph-mon@$(hostname)
# DOIT afficher : active

ceph -s
# DOIT afficher : mon: 1 daemons, quorum proxmox1
```

**Fix warnings :**

```bash
ceph config set mon auth_allow_insecure_global_id_reclaim false
ceph mon enable-msgr2
```

---

## ETAPE 4 : MGR (proxmox1 UNIQUEMENT)

```bash
pveceph mgr create
```

Si le service échoue :

```bash
systemctl reset-failed ceph-mgr@$(hostname)
systemctl start ceph-mgr@$(hostname)
```

**Vérification :**

```bash
ceph -s | grep mgr
# DOIT afficher : mgr: proxmox1(active, since ...)
```

---

## ETAPE 5 : OSD sur proxmox1

> ⚠️ Ne JAMAIS utiliser pveceph osd create (il rollback si le service échoue).
> Toujours utiliser prepare + activate séparément.

```bash
# Wipe agressif du disque
dmsetup remove_all 2>/dev/null
wipefs -af /dev/sdc
sgdisk --zap-all /dev/sdc
dd if=/dev/zero of=/dev/sdc bs=1M count=500
partprobe /dev/sdc
udevadm settle
sleep 5

# Préparer l'OSD (sans démarrer le service)
ceph-volume lvm prepare --data /dev/sdc

# Activer l'OSD
ceph-volume lvm activate --all

# Le premier start peut échouer, retry :
sleep 5
OSD_ID=$(ls /var/lib/ceph/osd/ | head -1 | sed 's/ceph-//')
systemctl reset-failed ceph-osd@$OSD_ID 2>/dev/null
systemctl start ceph-osd@$OSD_ID
```

**Vérification :**

```bash
ceph osd stat
# DOIT afficher : 1 osds: 1 up, 1 in

ceph osd tree
# DOIT montrer osd.0 sur proxmox1 en status UP
```

---

## ETAPE 6 : Bootstrap MON + MGR + OSD sur proxmox2 et proxmox3

> Sur chaque noeud secondaire. Le cluster doit être actif sur proxmox1.

**Sur proxmox2 (puis répéter identiquement sur proxmox3) :**

### 6a. Préparer l'accès config

```bash
ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf
cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring
chmod 644 /etc/ceph/ceph.client.admin.keyring

# Vérifier que ceph peut lire la config
sudo -u ceph cat /etc/ceph/ceph.conf | head -1
# DOIT afficher : [global]
```

### 6b. Bootstrap MON

```bash
# Récupérer le monmap du cluster
ceph mon getmap -o /tmp/monmap.bin

# Créer le datadir
mkdir -p /var/lib/ceph/mon/ceph-$(hostname)

# Bootstrap
ceph-mon --mkfs -i $(hostname) --monmap /tmp/monmap.bin --keyring /etc/ceph/ceph.client.admin.keyring

# Permissions
chown -R ceph:www-data /var/lib/ceph/mon/ceph-$(hostname)

# Démarrer (avec retry)
systemctl enable ceph-mon@$(hostname)
systemctl start ceph-mon@$(hostname)
sleep 10
systemctl is-active ceph-mon@$(hostname) || {
    systemctl reset-failed ceph-mon@$(hostname)
    systemctl start ceph-mon@$(hostname)
    sleep 10
}
```

**Vérification :**

```bash
systemctl is-active ceph-mon@$(hostname)
# DOIT afficher : active
```

### 6c. MGR

```bash
pveceph mgr create
sleep 5
systemctl is-active ceph-mgr@$(hostname) || {
    systemctl reset-failed ceph-mgr@$(hostname)
    systemctl start ceph-mgr@$(hostname)
}
```

### 6d. OSD

```bash
# Wipe
dmsetup remove_all 2>/dev/null
wipefs -af /dev/sdc
sgdisk --zap-all /dev/sdc
dd if=/dev/zero of=/dev/sdc bs=1M count=500
partprobe /dev/sdc
udevadm settle
sleep 5

# Prepare + activate (pas create !)
ceph-volume lvm prepare --data /dev/sdc
ceph-volume lvm activate --all

# Start avec retry
sleep 5
OSD_ID=$(ls /var/lib/ceph/osd/ | head -1 | sed 's/ceph-//')
systemctl reset-failed ceph-osd@$OSD_ID 2>/dev/null
systemctl start ceph-osd@$OSD_ID
```

### 6e. Vérification après chaque noeud

```bash
ceph -s
# Vérifier que le nombre de MON, OSD augmente
```

---

## ETAPE 7 : Création du pool (proxmox1, une seule fois)

```bash
# Attendre que les 3 OSD soient up
ceph osd stat
# DOIT afficher : 3 osds: 3 up, 3 in

# Créer le pool
ceph osd pool create ceph-vm 128 replicated
ceph osd pool set ceph-vm size 3
ceph osd pool set ceph-vm min_size 2
ceph osd pool application enable ceph-vm rbd

# Fix keyring (OBLIGATOIRE - sinon Permission denied)
ceph auth get-or-create client.admin \
    mon 'allow *' osd 'allow *' mds 'allow *' mgr 'allow *' \
    > /etc/ceph/ceph.client.admin.keyring
chmod 644 /etc/ceph/ceph.client.admin.keyring

# Ajouter à Proxmox avec keyring explicite
pvesm add rbd ceph-vm -pool ceph-vm -content images,rootdir -krbd 0 \
    -keyring /etc/ceph/ceph.client.admin.keyring
```

> ⚠️ Faire aussi sur les autres noeuds :
> ```bash
> ceph auth get-or-create client.admin mon 'allow *' osd 'allow *' mds 'allow *' mgr 'allow *' > /etc/ceph/ceph.client.admin.keyring
> chmod 644 /etc/ceph/ceph.client.admin.keyring
> systemctl restart pvestatd
> ```

---

## VERIFICATION FINALE

```bash
ceph -s
```

**Résultat attendu :**

```
cluster:
    health: HEALTH_OK

  services:
    mon: 3 daemons, quorum proxmox1,proxmox2,proxmox3
    mgr: proxmox1(active), standbys: proxmox2, proxmox3
    osd: 3 osds: 3 up, 3 in

  data:
    pools:   1 pools, 128 pgs
    objects: 0 objects, 0 B
    usage:   ~80 MiB used, ~381 GiB avail
    pgs:     128 active+clean
```

```bash
ceph osd tree
```

**Résultat attendu :**

```
ID  CLASS  WEIGHT   TYPE NAME          STATUS
-1         0.37198  root default
-3         0.12399      host proxmox1
 0    hdd  0.12399          osd.0      up
-5         0.12399      host proxmox2
 1    hdd  0.12399          osd.1      up
-7         0.12399      host proxmox3
 2    hdd  0.12399          osd.2      up
```

---

## Utilisation

- Créer une VM sur Ceph : **Create VM > Disk > Storage : ceph-vm**
- Migration live : **clic droit VM > Migrate > Online** (stockage partagé Ceph)
- HA : **VM > More > Manage HA** (failover automatique)

---

## Résumé des pièges et solutions

| Piège | Solution |
|-------|----------|
| /etc/pve/ceph.conf permissions | Overrides systemd --setgroup www-data |
| pveceph mon create (1er noeud) | Bootstrap manuel : monmaptool + ceph-mon --mkfs |
| pveceph mon create (2e/3e noeud) | ceph mon getmap + ceph-mon --mkfs |
| pveceph osd create rollback | ceph-volume lvm prepare + activate (séparé) |
| Service échoue au 1er start | systemctl reset-failed + systemctl start (retry) |
| pveceph install bloque sur Y/n | apt-get install directement |
| Disk "already in use" | dmsetup remove_all + wipefs -af + sgdisk --zap-all |
| Storage RBD "Permission denied" | Keyring dans /etc/ceph/ + chmod 644 + pvesm -keyring |
| rados_connect Permission denied | ceph auth get-or-create > /etc/ceph/ceph.client.admin.keyring |

## IMPORTANT : Fix Keyring pour le storage RBD

> ⚠️ Bug PVE 9 : le keyring dans /etc/pve/priv/ n'est pas lisible par les
> services Proxmox (pvestatd). Il faut copier le keyring dans /etc/ceph/.

**Sur CHAQUE noeud, après la création du pool :**

```bash
# Exporter le keyring admin lisible
ceph auth get-or-create client.admin \
    mon 'allow *' osd 'allow *' mds 'allow *' mgr 'allow *' \
    > /etc/ceph/ceph.client.admin.keyring
chmod 644 /etc/ceph/ceph.client.admin.keyring
```

**Lors de l'ajout du storage (ETAPE 7), utiliser `-keyring` :**

```bash
pvesm add rbd ceph-vm -pool ceph-vm -content images,rootdir -krbd 0 \
    -keyring /etc/ceph/ceph.client.admin.keyring
```

---

## Commandes utiles

```bash
ceph -s                     # Status global
ceph health detail          # Détails santé
ceph osd tree               # Arbre des OSD
ceph osd stat               # Statistiques OSD
ceph df                     # Espace disque
ceph osd pool ls detail     # Détails des pools
ceph mon stat               # Status des monitors
```

---

*Copyright (c) 2026 Ayi NEDJIMI Consultants*
*Testé sur Proxmox VE 9.1.6 + Ceph Squid 19.2.3 en nested Hyper-V*

# freeprox

Service pour **Proxmox VE** qui, dès qu'une **VM** ou un **conteneur LXC** est créé,
réserve automatiquement son adresse IP sur la **Freebox** (bail DHCP statique via l'API
Freebox OS) et y ajoute un commentaire descriptif, par exemple :

```
192.168.1.42   BC:24:11:5E:0A:31   [PVE] VM 105 web01 | pve/net0 | 4 vCPU, 8 Go, 32 Go | Linux | 2026-11-02
192.168.1.43   BC:24:11:9C:44:02   [PVE] LXC 210 dns01 | pve/net0 | 1 vCPU, 512 Mo, 8 Go | Debian | 2026-11-02
```

Aucune dépendance : Python 3 de la bibliothèque standard, déjà présent sur Proxmox.

## Fonctionnement

Proxmox n'expose pas d'événement « invité créé » (les *hookscripts* ne concernent que le
démarrage/arrêt d'un invité déjà configuré). freeprox tourne donc comme un service
systemd qui scrute `/etc/pve/.vmlist` et les fichiers de configuration du nœud
(toutes les 10 s par défaut) :

1. **Nouvel invité ou nouvelle carte réseau** sur un pont surveillé (`vmbr0` par défaut) :
   - attente que la création soit terminée (pas de `lock:` dans la config, config stable) ;
   - si un bail statique existe déjà pour ce MAC, il est conservé (commentaire mis à jour) ;
   - sinon l'IP est choisie dans cet ordre :
     1. l'IP statique déclarée dans l'invité (`ip=…` d'un LXC, `ipconfigN` cloud-init) ;
     2. l'IP que le DHCP de la Freebox lui a déjà attribuée ;
     3. la première IP libre du pool (ni réservée, ni louée, ni exclue, ne répondant pas au ping) ;
   - création du bail statique avec le commentaire.
2. **Invité supprimé** : optionnellement (`remove_on_destroy = true`), suppression des baux
   que freeprox a lui-même créés.
3. **Invité migré** vers un autre nœud du cluster : le service de ce nœud reprend le suivi
   et met à jour le commentaire (le bail, lié au MAC, ne change pas).

Les invités déjà présents lors du premier démarrage sont ignorés (voir `freeprox sync --all`).
Les modèles (templates) sont ignorés par défaut.

## Installation

Sur chaque nœud Proxmox (en root) :

```sh
git clone https://github.com/sudoPierre/freeprox.git
cd freeprox
./install.sh
```

### Associer freeprox à la Freebox

```sh
freeprox register
```

Validez la demande sur l'écran LCD de la Freebox. Ensuite, **obligatoire** : dans
Freebox OS → *Paramètres de la Freebox* → *Gestion des accès* → onglet *Applications*,
éditez **Freeprox** et cochez **« Modification des réglages de la Freebox »**
(nécessaire pour écrire les baux DHCP).

```sh
freeprox check                     # connexion, permission, config DHCP
systemctl enable --now freeprox
journalctl -u freeprox -f          # suivre l'activité
```

## Configuration

Fichier `/etc/freeprox/freeprox.conf` (voir [`etc/freeprox.conf`](etc/freeprox.conf),
toutes les options y sont documentées). Les plus utiles :

| Section | Option | Défaut | Rôle |
|---|---|---|---|
| `[proxmox]` | `bridges` | `vmbr0` | ponts reliés au LAN de la Freebox |
| `[dhcp]` | `pool_start` / `pool_end` | plage DHCP Freebox | où choisir les IP fixes |
| `[dhcp]` | `exclude` | — | IP/plages à ne jamais attribuer |
| `[comment]` | `template` | voir ci-dessus | format du commentaire |
| `[daemon]` | `remove_on_destroy` | `false` | libérer le bail à la suppression |
| `[daemon]` | `dry_run` | `false` | journaliser sans rien modifier |

Variables du gabarit de commentaire : `{type}` (`qemu`/`lxc`), `{type_label}` (`VM`/`LXC`),
`{vmid}`, `{name}`, `{node}`, `{iface}`, `{mac}`, `{bridge}`, `{ip}`, `{cores}`, `{memory}`,
`{memory_mib}`, `{disk}`, `{ostype}`, `{os}`, `{tags}`, `{date}` (date de création).

Astuce : gardez un pool d'IP fixes **hors** de la plage DHCP dynamique de la Freebox
(ex. réduire la plage DHCP à `.10-.99` et mettre `pool_start = 192.168.1.100`,
`pool_end = 192.168.1.199`).

## Commandes

```
freeprox register              associer l'application à la Freebox
freeprox check                 vérifier connexion, permissions et DHCP
freeprox daemon [-n]           service (lancé par systemd) ; -n = dry-run
freeprox sync [-n]             une seule passe
freeprox sync --all [-n]       réserver aussi les invités déjà existants
freeprox sync --vmid 105       (re)traiter un invité précis
freeprox leases [--mine]       lister les baux statiques
freeprox release 105 [...]     supprimer les baux créés pour un invité
```

## Remarques

- Le bail est réservé dès la création : l'invité obtiendra l'IP à sa prochaine requête DHCP
  (donc dès son premier démarrage s'il est en DHCP).
- Pour un invité configuré en IP statique, la réservation garantit simplement que la
  Freebox n'attribuera pas cette IP à un autre appareil.
- Accès en HTTPS : `url = https://mafreebox.freebox.fr` avec `ca_file` pointant vers les
  certificats racine Freebox, ou `verify_tls = false`.
- Désinstallation : `./uninstall.sh` (`--purge` pour supprimer aussi config et jeton).
  Les baux existants restent sur la Freebox.

## Développement

```sh
python3 -m unittest discover -s tests -t .
```

Les tests d'intégration utilisent un faux serveur Freebox OS et une fausse arborescence
`/etc/pve` (`tests/fakes.py`).

## Licence

MIT

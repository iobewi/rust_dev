# Qualification A/B/C des lectures flash — ESP32-P4

Objectif : comparer le **débit de lecture flash**, non la vitesse nominale du port
USB, pour la **même carte, même câble, même port, même offset et même volume**.

## État de référence : essai A déjà effectué

- Outil : IDLab (esptool-js 0.7.0 via Web Serial), carte `esp32-local`.
- Commande : `flash.read`, adresse `0x10000`, taille `0x100000` (1 Mio).
- Durée utile : **23 106 ms** ; débit **45 381 octets/s**.
- SHA-256 : `a5bd08050eb97eb0e3c223deeead165e22c3cdfd59dcea691fbaa56980c50bf0`.
- Répétition antérieure : même SHA-256, durée 23 108 ms.
- Transfert observé : latence ACK → premières données **88,9 ms** en moyenne.
- Conserver comme référence **A**, et ne pas la confondre avec une nouvelle lecture.

## Prérequis et arrêt de sécurité

Ce protocole n'a pas été exécuté matériellement dans le cadre de cette PR.
Avant B : vérifier l'identité de la carte P4 et le VID/PID du port en usage,
l'absence d'opération, réservation, grant ou récupération en cours. Dans IDLab,
**déconnecter la carte concernée** via la commande de déconnexion du banc,
puis confirmer qu'elle ne détient plus le port. Ne pas arrêter l'ensemble du
serveur ni déconnecter les autres cartes pour cela. Le port libéré doit
correspondre à celui de la carte P4 ; une désignation COM peut changer après
ré-énumération : **ne pas supposer que le numéro est resté identique**.

Ne jamais exécuter `esptool` et Web Serial simultanément sur le même port.
Une lecture peut provoquer l'entrée en bootloader ou le chargement d'un stub
RAM ; elle n'écrit ni n'efface la flash, mais elle peut perturber le firmware
en cours. La commande est explicitement limitée à `read-flash`. Arrêter si
identification ambiguë, déconnexion USB, erreur flash ou mismatch SHA-256.
Les données flash et journaux peuvent être sensibles : les garder localement,
hors du dépôt, et ne partager que les tailles, durées et empreintes.

## Essai B : esptool Python 5.6.0 sous Windows

Utiliser un **Python natif Windows** pour accéder directement à `COMx` :
WSL2 n'expose pas le même port par défaut. Dans PowerShell, depuis la racine
du dépôt sur le commit approuvé de la PR #67 :

```powershell
py -3 -m venv .venv-esptool
.\.venv-esptool\Scripts\python.exe -m pip install "esptool==5.6.0"
.\.venv-esptool\Scripts\python.exe -m esptool version

# Sans --execute : affiche seulement la commande, sans ouvrir le port.
.\.venv-esptool\Scripts\python.exe bench/tools/compare_flash_read.py

# Après libération du port et vérification du COM réel (exemple COM9).
.\.venv-esptool\Scripts\python.exe bench/tools/compare_flash_read.py --execute --port COM9 --outdir "$env:USERPROFILE\IDLab-evidence\P4-read-B"
```

Le dossier de sortie doit être **nouveau ou vide**. Remplacer COM9 par le port
USB vérifié. Le script crée `esp32p4-python-B-0x10000-1MiB.bin`,
`esptool-B.log` et `report-B.json`.

Il utilise `--chip esp32p4 --baud 115200 --after no-reset read-flash`
avec le stub normalement activé. **Ne pas utiliser `--no-stub`**
pour cette comparaison, car le protocole de lecture est différent.
Le paramètre 115200 est nominal pour USB natif ; il ne mesure pas le débit
physique USB. Ne pas changer simultanément le câble, le port, le contrôleur
(OTG vs Serial/JTAG), le firmware ou la version d'esptool.

`report-B.json` fournit deux durées distinctes :

- `esptool_read_elapsed_s` : temps de lecture annoncé par esptool ;
  à comparer aux **23,106 s d'IDLab**.
- `wall_elapsed_ms` : inclut connexion, identification et chargement stub ;
  **ne pas comparer directement** à la durée de lecture IDLab.

Vérifier la taille exacte (1 048 576 octets), le code retour, l'empreinte
SHA-256, la version esptool, l'état du stub et la nature du port.
Si la commande ne peut pas ouvrir le port ou atteindre le bootloader,
arrêter et examiner le journal : aucun reset ou nouvelle tentative implicite
n'est autorisé par la recette.

## Essai C : contrôle IDLab après B

Fermer esptool Python, vérifier que son processus a libéré le port,
reconnecter cette même carte à IDLab, puis effectuer `flash.read` à
`0x10000` sur `0x100000` octets. Noter le SHA-256 et les métriques
`result.transfer` de la nouvelle opération. Confirmer les informations
d'identité USB et du stub. Ne pas confondre l'essai C avec une ancienne
opération dans l'historique.

## Critères de verdict

- **Intégrité PASS** si A, B et C ont tous une taille de 1 Mio et un SHA-256
  exactement identique. Sinon **FAIL** ou **INDETERMINATE** si comparaison
  incomplète. Des SHA identiques établissent la reproductibilité, pas une
  preuve indépendante que le contenu physique de la flash est correct.
- **Performance** : comparer les secondes de **lecture utile**, pas le temps
  de connexion. Calculer le rapport `23,106 / esptool_read_elapsed_s`.
- **Interprétation** : un Python nettement plus rapide oriente l'analyse vers
  le client JavaScript/Web Serial et ses dépendances ; des temps proches
  orientent l'étude vers le stub, le chemin USB et la configuration matérielle.
  Cette comparaison, à elle seule, n'isole pas un composant responsable.
- **État matériel final** : noter si la carte revient en mode prévu et si
  le banc a récupéré la console sans reconfiguration destructive.

## Sources

- https://docs.espressif.com/projects/esptool/en/latest/esp32p4/esptool/basic-commands.html
- https://docs.espressif.com/projects/esptool/en/latest/esp32p4/esptool/basic-options.html
- https://github.com/espressif/esptool/blob/master/esptool/cmds.py

L'exécution matérielle exige l'accord et les vérifications prévus par
`bench/docs/qualification-workflow.md`; cette documentation et les tests
logiciels ne se substituent pas aux contrôles de session.

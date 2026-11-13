# Port USB natif de l'ESP32-P4 : entrée manuelle en mode téléchargement (agent)

**Statut : changement logiciel, testé avec des doublures et par contrôles statiques uniquement dans ce dépôt. Les constats sur la carte réelle viennent de sessions de qualification privées (hors Git) et sont signalés comme tels.**

## Constat (carte réelle, ESP32-P4 ECO7 v3.2)
- Le port USB « data » du P4, en mode téléchargement ROM (BOOT enfoncé, RESET, relâcher BOOT), s'énumère en **`303a:0012`** (USB-OTG de la ROM : le PID est l'identifiant de puce 18), pas en `303a:1001` (USB-Serial-JTAG). Sans BOOT + RESET manuels, aucun port n'apparaît. Le banc le classe `native_usb_other` ; ce port ne lui donne ni `reset`, ni `io0`, ni `boot.download`.
- L'agent créait son chargeur à 460800 baud : après le chargement du stub, esptool-js change le débit et rouvre le port, ce qui n'a pas de sens sur un port USB. La connexion restait sur « Connexion… ». Une démo esptool-js hors banc (débit nominal 115200, sans changement) détecte la puce par ce port.
- Un reset fait sortir la puce du mode téléchargement ; sans application, le port disparaît. La réouverture de la console après l'identification (`openConsole()`) ferme et rouvre le port : la première commande suivante a échoué en 41 ms avec `serial_disconnected`. La cause exacte du reset n'a pas été observée (déduction du code).

## Changements
1. **Débit du chargeur** : `flashBaud` vaut 115200 pour tout port natif `303a:*` (aucun changement de débit) ; un pont UART externe (CH343, CP210x…) garde 460800.
2. **Case « Entrée manuelle »** (`#manual-download`, à côté de « Ajouter une carte ») : décochée par défaut, non mémorisée, lue à la création du moteur de la carte. Cochée **et** port `303a:*` :
   - esptool-js se connecte en `no_reset` (aucune séquence de reset, simple synchronisation) ; un événement de transport l'annonce ;
   - la session de chargeur **reste ouverte** : `openConsole()` ne rouvre plus la console après l'identification ni après une commande `return_console`. Seule une commande explicite `console` ou `uart.configure` la quitte.
3. **Case lisible** : elle apparaissait comme un petit point bleu, car `button,input,select{min-width:0}` et `input[type=checkbox]{min-height:0}` la laissaient se comprimer dans le libellé. Zone de 18 px qui ne rétrécit pas, alignée sur la première ligne, libellé entièrement cliquable (`label for` + `span`), états survol, focus (contour de 2 px) et désactivé. Toutes les règles sont limitées à `.manual-download` (bloc `manual-download:start/end` de `style.css`).
4. `bench/.gitignore` : `node_modules` (sans barre finale) pour couvrir aussi un lien symbolique ou un fichier de ce nom.

Sans la case cochée, rien ne change, sur aucun port.

## Limites
- Aucun reset entre deux commandes : un redémarrage de la puce (BOOT + RESET), un débranchement ou un rechargement de la page exigent de reconnecter la page, case cochée. Le banc ne peut pas ouvrir une nouvelle session de chargeur de lui-même sur ce port.
- Le débit de lecture mesuré sur la carte par ce port (environ 45 Ko/s) est limité par le stub officiel (un paquet de 4096 octets à la fois, avec acquittement), pas par l'USB.
- Ne qualifie ni l'écriture, ni l'effacement, ni la restauration par ce port. L'identité du stub (`stub_identity`) et le relevé de l'entrée utilisée appartiennent à la chaîne des diagnostics P4 (non incluse ici).

## Vérification
`tests/manual-download.test.mjs` (débit, mode d'entrée, maintien de session, absence d'effet sur les ponts UART) et `tests/manual-download-ui.test.mjs` (balisage, câblage, portée du CSS). Le rendu n'est pas vérifiable par navigateur dans le devcontainer : à contrôler sur la page rechargée (Ctrl+Shift+R) : case carrée d'environ 18 px, visible cochée et décochée ; clic sur le texte qui la coche ; contour de focus avec Tab ; texte à la ligne sans que la case rétrécisse dans le panneau étroit ; aucun autre contrôle modifié.

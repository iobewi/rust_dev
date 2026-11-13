# Port USB natif de l'ESP32-P4 : entrée manuelle en mode téléchargement (agent)

**Statut : changement logiciel, testé avec des doublures et par contrôles statiques uniquement dans ce dépôt. Les constats sur la carte réelle viennent de sessions de qualification privées (hors Git) et sont signalés comme tels.**

## Constat (carte réelle, ESP32-P4 ECO7 v3.2)
- Le port USB « data » du P4, en mode téléchargement ROM (BOOT enfoncé, RESET, relâcher BOOT), s'énumère en **`303a:0012`** (USB-OTG de la ROM : le PID est l'identifiant de puce 18), pas en `303a:1001` (USB-Serial-JTAG). Sans BOOT + RESET manuels, aucun port n'apparaît. Le banc le classe `native_usb_other` ; ce port ne lui donne ni `reset`, ni `io0`, ni `boot.download`.
- L'agent créait son chargeur à 460800 baud : après le chargement du stub, esptool-js change le débit et rouvre le port, ce qui n'a pas de sens sur un port USB. La connexion restait sur « Connexion… ». Une démo esptool-js hors banc (débit nominal 115200, sans changement) détecte la puce par ce port.
- Un reset fait sortir la puce du mode téléchargement ; sans application, le port disparaît. La réouverture de la console après l'identification (`openConsole()`) ferme et rouvre le port : la première commande suivante a échoué en 41 ms avec `serial_disconnected`. La cause exacte du reset n'a pas été observée (déduction du code).

## Changements
1. **Débit du chargeur** : `flashBaud` vaut 115200 pour tout port natif `303a:*` (aucun changement de débit) ; un pont UART externe (CH343, CP210x…) utilise 460800 par défaut, ou le réglage propre à la carte.
2. **Case « Entrée manuelle »** (`#manual-download`, à côté de « Ajouter une carte ») : décochée par défaut, capturée pour la carte ajoutée, conservée lors de ses reconnexions. Cochée sur USB natif **ou** pont UART :
   - esptool-js se connecte en `no_reset` (aucune séquence de reset, simple synchronisation) ; un événement de transport l'annonce ;
   - la session de chargeur **reste ouverte** : `openConsole()` ne rouvre plus la console après l'identification ni après une commande `return_console`. Seule une commande explicite `console` ou `uart.configure` la quitte.
3. **Case lisible** : elle apparaissait comme un petit point bleu, car `button,input,select{min-width:0}` et `input[type=checkbox]{min-height:0}` la laissaient se comprimer dans le libellé. Zone de 18 px qui ne rétrécit pas, alignée sur la première ligne, libellé entièrement cliquable (`label for` + `span`), états survol, focus (contour de 2 px) et désactivé. Toutes les règles sont limitées à `.manual-download` (bloc `manual-download:start/end` de `style.css`).
4. `bench/.gitignore` : `node_modules` (sans barre finale) pour couvrir aussi un lien symbolique ou un fichier de ce nom.

Sans la case cochée, rien ne change, sur aucun port.

## Limites
- Aucun reset entre deux commandes en entrée manuelle. Après débranchement, la reconnexion reprend les réglages de cette carte ; elle ne replace pas physiquement la puce en téléchargement. Après rechargement de la page, l’opérateur sélectionne de nouveau les options lors de l’ajout.
- Le débit de lecture mesuré sur la carte par ce port (environ 45 Ko/s) est limité par le stub officiel (un paquet de 4096 octets à la fois, avec acquittement), pas par l'USB.
- Ne qualifie ni l'écriture, ni l'effacement, ni la restauration par ce port. L'identité du stub (`stub_identity`) et le relevé de l'entrée utilisée appartiennent à la chaîne des diagnostics P4 (non incluse ici).

## Vérification
`tests/manual-download.test.mjs` (débit, mode d'entrée, maintien de session, entrée manuelle sur les ponts UART) et `tests/manual-download-ui.test.mjs` (balisage, câblage, portée du CSS). Le rendu n'est pas vérifiable par navigateur dans le devcontainer : à contrôler sur la page rechargée (Ctrl+Shift+R) : case carrée d'environ 18 px, visible cochée et décochée ; clic sur le texte qui la coche ; contour de focus avec Tab ; texte à la ligne sans que la case rétrécisse dans le panneau étroit ; aucun autre contrôle modifié.

## Réglages par carte : USB natif et pont UART

Le choix est capturé **avant** le sélecteur USB et stocké dans le contexte de
la carte ajoutée. Une reconnexion automatique ou volontaire reprend ce contexte,
sans relire les contrôles du formulaire d'ajout d'une autre carte. La case et
la vitesse du formulaire sont réinitialisées après un ajout réussi ; les options
de la carte existante restent inchangées. Elles ne persistent pas après un
rechargement de la page.

L'entrée manuelle s'applique aussi aux ponts UART sans câblage DTR/RTS. Avec cette
option, `no_reset` et le maintien de la session de chargeur s'appliquent aux deux
transports. L'opérateur doit avoir placé la puce en téléchargement. Les ponts
avec DTR/RTS conservent leur séquence automatique lorsque la case est décochée.

Le champ **Vitesse du chargeur UART** accepte Automatique, 115200, 230400,
460800 et 921600. Automatique conserve 460800 sur UART. L'USB natif reste à
115200 nominal, même si une vitesse UART différente a été choisie. Le réglage
ne modifie pas le débit de la console applicative et ne déclenche aucune
opération sur les cartes déjà ajoutées.

Validation logicielle ciblée : tests `manual-download`, `manual-download-ui`,
`board-manager` et `engine`. Les doubles vérifient les décisions du moteur et
l'isolation des options ; ils ne prouvent ni le câblage réel ni les signaux
physiques, et ne qualifient pas le comportement d'esptool-js sur la carte.

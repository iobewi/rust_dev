# Interface d’instrumentation

La page connecte les cartes, présente le transport brut et demande les opérations au serveur. Les recettes, assertions et preuves durables restent dans les projets clients.

## Connexion locale sans token

En accès local par `localhost`, `127.0.0.1` ou `::1` (direct ou redirection locale explicitement déclarée), la page ouvre automatiquement sa session sur le serveur. Un cookie HttpOnly, SameSite Strict, partagé par REST et WebSocket, remplace les deux champs de tokens. Le cookie ne contient aucune clé API externe et n’est pas accessible au JavaScript. La session est éphémère, expire après huit heures et disparaît au redémarrage serveur.

En mode direct, le serveur vérifie la connexion réseau de boucle locale, le Host local et l’origine exacte des mutations et du WebSocket. Pour un devcontainer ou un relais local, `BENCH_UI_FORWARD_PEERS` déclare les pairs de confiance et `BENCH_ORIGIN` l’URL locale exacte du navigateur. Cela permet une adresse interne et un Host réécrit différents, sans faire confiance aux en-têtes transférés. Les requêtes de site tiers et les relais non déclarés restent refusés. L’ouverture automatique n’est pas disponible par un accès LAN ou un nom DNS arbitraire non authentifié. Un déploiement réseau avec authentification utilisateur n’est pas implémenté dans cette version ; aucune session privilégiée n’est accordée simplement parce que le serveur fournit la page.

**Connecter**, dans le groupe USB de la barre latérale, reste le seul choix matériel nécessaire. Le même bouton devient **Déconnecter** quand le port est ouvert. Un port déjà autorisé est réutilisé s’il est unique ; sinon le sélecteur Web Serial est présenté. Pendant une ouverture ou fermeture, le bouton et les actions matérielles sont désactivés pour éviter les doubles actions. Pendant une reconnexion automatique, il indique **Reconnexion…** pendant la tentative ou **Annuler la reconnexion** pendant l’attente. Une déconnexion volontaire désactive la reprise automatique. Le navigateur demande sa propre permission Web Serial, identifie la carte puis ouvre la console si aucun blocage n’est mémorisé. La liaison agent au serveur s’établit automatiquement. **Paramètres du banc…** conserve l’identifiant logique autorisé et les origines HTTP LAN ; **Rétablir la liaison au serveur** sert à une reconnexion explicite après une interruption réseau.

Les identifiants de cartes disponibles viennent de la configuration `devices` des agents ; en l’absence d’agent externe configuré, le serveur utilise `esp32-local`. Un client externe tel que Claude conserve son token REST indépendant et ses scopes. Le navigateur ne le reçoit pas et ne le stocke pas.

## Commandes directes

Il n’y a plus de bouton « Prendre la main », de renouvellement manuel ou de délégation nécessaire avant une action. Un clic sur Identifier, Lire, Reset ou une autre commande envoie une opération manuelle au serveur. Celui-ci prend atomiquement une réservation exclusive, vérifie la génération de carte, puis utilise la même file et le même agent que les clients externes.

La réservation est libérée après réception du résultat par le serveur et confirmation par l’agent que le traitement de l’accusé et le retour console sont terminés. Elle ne disparaît pas prématurément pendant une réouverture de port. Une commande annulée ou expirée avant départ libère sa réservation ; une issue matérielle non stabilisée conserve les protections d’exclusion et de quarantaine.

Les actions restent refusées si un autre client possède la carte. La page affiche son propriétaire et son activité. « Client bloqué » permet à l’opérateur physique de libérer une réservation abandonnée après confirmation, sans redémarrer le serveur ; cette action refuse un matériel encore en cours de traitement et ne lève pas la quarantaine.

Un déclencheur armé conserve automatiquement sa réservation jusqu’à son expiration ou à son désarmement. La page peut le désarmer ou effectuer d’autres commandes dans cette réservation ; aucun renouvellement manuel n’est demandé. Les accès matériels restent sérialisés.

## Une confirmation pour les actions locales sensibles

Le plan flash conserve une confirmation explicite de la carte, de sa génération et des régions. L’effacement global possède une confirmation distincte. Ces confirmations autorisent uniquement l’opération manuelle adressée à l’agent physique de la même session ; elles n’ouvrent aucun grant temporaire utilisable par Claude.

Les commandes locales UART, BOOT, HTTP et déclencheurs ne nécessitent plus un bouton préalable d’accès privilégié. Les contrôles serveur, le choix explicite de commande et les restrictions matérielles restent applicables. Une carte en quarantaine reste limitée aux commandes de vérification autorisées.

Pour les clients externes, la réservation, le scope `program`, le consentement `allow_destructive:true` et le grant local restent nécessaires selon la commande. La bande **Autorisations** affiche directement la durée et **Autoriser les opérations sensibles à distance**, sans sous-menu. Elle permet de choisir 1 minute, 5 minutes par défaut, 30 minutes ou 2 heures. La révocation reste directement accessible pendant le grant ; la barre affiche « Distant autorisé · N s ». Au repos, le libellé « Autorisations » et l’état « À la demande » évitent de présenter toute la carte comme verrouillée. Une autorisation distante n’est jamais créée automatiquement par un clic sur une commande locale.

## Consoles et outils

Deux onglets principaux séparent les usages : **Consoles** présente la console UART contrastée et redimensionnable ; **Opérations** rassemble Partitions, Plan flash, Lecture mémoire, Historique des opérations et Déclencheurs/outils. Le **journal de l’agent est commun aux deux vues**, en bas de la zone de travail, sous le contenu de l’onglet actif : sa progression reste visible pendant une lecture flash dans Opérations. Changer d’onglet conserve le même journal et son état réduit/développé. La capture UART continue quand son onglet est masqué. Un résultat de lecture ou une erreur ouvre automatiquement le panneau concerné. Les deux niveaux d’onglets sont utilisables avec les flèches, Début et Fin.

Le groupe **USB** est tout en haut de la barre latérale et reste visible. Avant la connexion locale, seul ce groupe est affiché : le nom de carte et ses commandes sont masqués. La connexion développe automatiquement les détails ; un débranchement ou une déconnexion volontaire les masque, puis la reconnexion les réaffiche. Une carte connectée par un agent externe reste consultable même sans port USB local.

La carte sélectionnée, son identité, son mode et les commandes Identifier, Retour console, Reset EN et BOOT restent dans une **barre latérale permanente**, commune aux onglets Consoles et Opérations. Les boutons sont regroupés par rôle : **Identification** (informations et Identifier), **Mode et redémarrage** (mode courant, Retour console et Reset USB pour un contrôleur Serial/JTAG reconnu), **Signaux matériels** (Reset EN et maintien BOOT). Les explications de réservation automatique ne sont plus affichées dans ce panneau ; les aides des actions concernées conservent l’avertissement de redémarrage. La récupération matérielle y est également disponible ; le titre indique « Carte bloquée » en cas de quarantaine. Sur ordinateur, le panneau suit le défilement sous l’en-tête ; sur écran étroit, il se place au-dessus du contenu sans être masqué.

Sur le contrôleur USB Serial/JTAG `303a:1001`, **Reset USB** apparaît dans Mode et redémarrage, tandis que EN/IO0 restent non exposés et BOOT reste désactivé. Sur pont UART, **Reset EN** reste dans Signaux matériels. Le reset est grisé hors du mode console et ses protections de concurrence/quarantaine restent applicables. Les autres ports USB natifs ne reçoivent pas cette capacité par simple correspondance du VID. L’identité USB affichée distingue ces ports du contrôleur Serial/JTAG.

Le bouton **Retour console** est grisé par défaut et ne devient disponible que pour la carte locale dont le moteur confirme le mode chargeur. Il reste désactivé pendant une opération, en cas de réservation concurrente, de déconnexion ou de quarantaine. Un agent externe dont le mode n’est pas observable ne l’active pas. Le retour peut redémarrer la carte ; les opérations chargeur reviennent normalement d’elles-mêmes en console.

La connexion USB et son état sont toujours visibles dans la barre latérale, avec un seul bouton selon l’état du port. La barre supérieure conserve **Agent** pour la liaison et les paramètres, **Pilotage** pour le propriétaire et la libération d’une réservation abandonnée, **Autorisations** pour les clients distants et **Veille** pour le maintien d’écran. Les icônes, libellés et couleurs évoluent avec l’état réel. Les états de carte ne sont plus derrière une icône à ouvrir.

Le survol ou le focus affiche une explication ; un clic déplie les détails et actions dans une bande sous l’en-tête, qui pousse le contenu sans le recouvrir. Une seule bande est ouverte à la fois : cliquer sur une autre icône change son contenu ; un second clic sur la même icône, Échap ou Fermer la replie. Un clic dans le reste de la page la laisse ouverte. Ces actions ne démarrent ni n’arrêtent aucune opération matérielle. La bande est accessible au clavier et au toucher. Les autres aides restent disponibles via les boutons « ? ».

Si la lecture à `0x8000` échoue avec `partition_table_invalid`, l’onglet Partitions propose **Essayer 0x10000**. Ce clic met à jour l’adresse et lance une nouvelle lecture via la même API, sans écriture ni recherche automatique. La suggestion reste liée à la carte et à sa génération ; elle ne signifie pas qu’une table a déjà été détectée à `0x10000`. Les erreurs de transport et les autres adresses ne déclenchent pas cette proposition.

La console prend par défaut 55 % de la hauteur de la fenêtre et suit son redimensionnement. Sa hauteur reste réglable manuellement par son bord inférieur ; **Hauteur auto** rétablit la taille proportionnelle. Sur une petite fenêtre, les limites de hauteur restent relatives à la fenêtre.

`ConsoleBuffer` conserve jusqu’à 1 Mio / 4 096 événements par carte. Les lignes texte suivent les fins de ligne du firmware (LF, CRLF, CR), et non les fragments USB. Une ligne partielle reste visible et se complète en direct. Horodatage et séquence correspondent au premier fragment retenu de la ligne. La recherche porte sur les lignes assemblées. Hex, pause, recherche et effacement de l’affichage ne changent pas l’export brut. Le retour au direct est explicite après consultation de l’historique.

Le journal de l’agent conserve 300 lignes : connexions, modes console/chargeur, commandes, progression, transferts, résultats et durées. Les erreurs indiquent la phase, le type de transport et les délais restants. Les nouveaux diagnostics n’exposent ni tokens, ni arguments, ni texte arbitraire d’exception. Le flux UART peut lui-même contenir des données sensibles du firmware.

Les lectures demandent au plus dix minutes et utilisent des blocs internes d’au plus 1 Mio. Aucun bloc échoué n’est rejoué et aucun dump partiel n’est publié. Le téléchargement vérifie la taille et le SHA-256.

## Reconnexion et anti-veille

Après débranchement/rebranchement, seul le port précédemment choisi est repris automatiquement si le navigateur le reconnaît. Puce, capacité, RDID et MAC sont revérifiés avant retour console. Aucune opération interrompue n’est rejouée. Un blocage persistant interdit le retour console automatique ; une déconnexion volontaire désactive la reprise. Ouvrir le chargeur ou revenir à la console peut redémarrer la carte.

Screen Wake Lock demande le maintien d’écran pendant l’activité de l’agent, jusqu’au traitement de l’accusé et du retour console. Il est libéré au repos, à la déconnexion ou au départ de page. La visibilité, la réacquisition et les refus du navigateur sont signalés ; cela ne garantit pas l’exécution d’un onglet masqué.

## Validation

Les tests serveur couvrent sessions locales, origine/Host, cookies, expiration, maintien des accès Bearer, concurrence, autorité locale limitée à son agent, confirmation destructive et libération automatique après stabilisation. Les parcours Chromium utilisent un port et un chargeur simulés, avec les vraies interfaces REST et WebSocket.

Les retours matériels sur les SHA antérieurs ne qualifient pas cette évolution de session. Les cas non exécutés de l’issue #2 restent ouverts, notamment programmation, autres cartes/interfaces, onglet masqué et libération effective de l’anti-veille. Les anciens resets et l’échec historique vers 350 s n’ont pas de cause isolée démontrée.

# Interface d’instrumentation

La page connecte **plusieurs cartes** dans une seule fenêtre, présente le transport brut de la carte sélectionnée et demande les opérations au serveur. Les recettes, assertions et preuves durables restent dans les projets clients.

## Parcours minimal

1. **Démarrer le serveur** (voir le README) : `BENCH_TOKENS_FILE=… python3 bench/tools/run_server.py`, puis vérifier `GET /health`.
2. **Ouvrir l'interface** sur le PC qui possède les ports USB : `http://127.0.0.1:8080` (navigateur de bureau avec Web Serial : Chrome, Edge ou Firefox 151+ ; voir la qualification dans le README). Aucun token à saisir.
3. **Ajouter une carte** : choisir son identifiant dans **Cartes actives**, cliquer **Ajouter une carte**, sélectionner le port. Répéter pour la deuxième carte.
4. **Identifier et sélectionner** : cliquer sur le nom d'une carte ; **Identifier** (zone Identification) lit puce, flash, RDID et MAC de la carte sélectionnée.
5. **Utiliser les commandes** : console, reset, BOOT, lecture mémoire… agissent sur la carte sélectionnée ; chaque opération prend et libère sa propre réservation.
6. **Déconnecter proprement** : cliquer sur le statut **Connectée** de la carte (pas de commande en cours), attendre **Déconnectée**, puis **Retirer** si besoin. Fermer la page seulement ensuite.

## Connexion locale sans token

En accès local par `localhost`, `127.0.0.1` ou `::1` (direct ou redirection locale explicitement déclarée), la page ouvre automatiquement sa session sur le serveur. Un cookie HttpOnly, SameSite Strict, partagé par REST et WebSocket, remplace les deux champs de tokens. Le cookie ne contient aucune clé API externe et n’est pas accessible au JavaScript. La session est éphémère, expire après huit heures et disparaît au redémarrage serveur.

En mode direct, le serveur vérifie la connexion réseau de boucle locale, le Host local et l’origine exacte des mutations et du WebSocket. Pour un devcontainer ou un relais local, `BENCH_UI_FORWARD_PEERS` déclare les pairs de confiance et `BENCH_ORIGIN` l’URL locale exacte du navigateur. Cela permet une adresse interne et un Host réécrit différents, sans faire confiance aux en-têtes transférés. Les requêtes de site tiers et les relais non déclarés restent refusés. L’ouverture automatique n’est pas disponible par un accès LAN ou un nom DNS arbitraire non authentifié. Un déploiement réseau avec authentification utilisateur n’est pas implémenté dans cette version ; aucune session privilégiée n’est accordée simplement parce que le serveur fournit la page.

**Ajouter une carte**, dans la section **Cartes actives** de la barre latérale, est le seul choix matériel nécessaire : choisir un identifiant logique libre et autorisé, puis le port dans le sélecteur Web Serial du navigateur. Chaque clic ouvre ce sélecteur, y compris quand un seul port est déjà autorisé ; aucune association automatique par VID/PID ni nom de MCU n'est faite. Un identifiant déjà utilisé ou un port déjà revendiqué par une autre entrée est refusé avant la création d'un moteur matériel. Le navigateur identifie la carte puis ouvre la console si aucun blocage n'est mémorisé ; la liaison agent au serveur s'établit automatiquement, un agent WebSocket par carte, limité à l'identifiant logique autorisé.

La liste **Cartes actives** n'affiche que les cartes connectées ou précédemment connectées dans cette fenêtre, sans liste déroulante. Chaque entrée montre le nom logique, la puce et un statut. **Cliquer sur le nom** sélectionne la carte (surbrillance) : Identification, Commandes, consoles, opérations, historique et journal montrent cette carte, sans déconnexion ni commande matérielle. Le **statut est un bouton**, distinct du nom :

| Statut | Signification | Clic sur le statut |
|---|---|---|
| **Connectée** | port ouvert, agent enregistré | déconnecte uniquement ce port ; refusé pendant une opération, confirmation si une réservation, un grant ou un blocage existe |
| **Connexion…** / **Reconnexion…** | ouverture ou tentative en cours | désactivé |
| **USB perdu** / **Reconnexion en attente** | câble retiré ; seul le port précédemment choisi sera repris | annule la reconnexion de cette carte, qui passe en **Déconnectée** |
| **Déconnectée** | déconnexion volontaire ou reconnexion annulée | reconnecte au port précédemment sélectionné, puis revérifie puce, capacité flash, RDID et MAC ; **Retirer** oublie l'entrée dans cette fenêtre sans supprimer les preuves serveur |

Une déconnexion volontaire annule les déclencheurs, révoque le grant, ferme le WebSocket agent et ferme réellement le port Web Serial après libération des verrous de lecture/écriture ; l'état déconnecté n'est affiché qu'après cette fermeture. Le port mémorisé pour la reprise automatique est oublié, y compris en cas d'événement USB tardif. Déconnecter, perdre ou reconnecter une carte ne modifie ni la sélection en cours ni l'autre carte. **Paramètres du banc…** conserve les origines HTTP LAN ; **Rétablir la liaison au serveur** sert à une reconnexion explicite après une interruption réseau.

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

La section **Cartes actives** est tout en haut de la barre latérale et reste visible. Tant qu'aucune carte n'est connectée, seule cette section est affichée ; la première connexion développe les détails de la carte sélectionnée. Les ports USB de l'ancienne interface (sections « Carte locale » et « USB ») n'existent plus. Une carte connectée par un agent externe reste consultable sans port USB local.

La carte sélectionnée, son identité et les commandes Identifier, Retour console, Reset EN et BOOT restent dans une **barre latérale permanente**, commune aux onglets Consoles et Opérations. Les boutons sont regroupés par rôle : **Identification** (informations et Identifier), **Commandes de la carte** (Retour console, reset et maintien BOOT). Retour console reste visible mais grisé hors du mode chargeur ; il remplace l’affichage séparé du mode. Seules les commandes matérielles prises en charge par la connexion sont affichées. Les explications de réservation automatique ne sont plus affichées dans ce panneau ; les aides des actions concernées conservent l’avertissement de redémarrage. La récupération matérielle y est également disponible ; le titre indique « Carte bloquée » en cas de quarantaine. Sur ordinateur, le panneau suit le défilement sous l’en-tête ; sur écran étroit, il se place au-dessus du contenu sans être masqué.

Sur le contrôleur USB Serial/JTAG `303a:1001`, **Reset USB** apparaît dans Commandes de la carte et les contrôles BOOT (durée comprise) sont masqués. Sur pont UART, la même section affiche **Reset EN** et **Maintenir BOOT**. Un port ne proposant pas de reset n’affiche pas cette commande. Le reset est grisé hors du mode console et ses protections de concurrence/quarantaine restent applicables. Les autres ports USB natifs ne reçoivent pas cette capacité par simple correspondance du VID. L’identité USB affichée distingue ces ports du contrôleur Serial/JTAG.

Le bouton **Retour console** est grisé par défaut et ne devient disponible que pour la carte locale dont le moteur confirme le mode chargeur. Il reste désactivé pendant une opération, en cas de réservation concurrente, de déconnexion ou de quarantaine. Un agent externe dont le mode n’est pas observable ne l’active pas. Le retour peut redémarrer la carte ; les opérations chargeur reviennent normalement d’elles-mêmes en console.

L'état USB de chaque carte est toujours visible dans **Cartes actives**. La barre supérieure conserve **Agent** pour la liaison et les paramètres, **Pilotage** pour le propriétaire et la libération d’une réservation abandonnée, **Autorisations** pour les clients distants et **Veille** pour le maintien d’écran. Les icônes, libellés et couleurs évoluent avec l’état réel. Les états de carte ne sont plus derrière une icône à ouvrir.

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

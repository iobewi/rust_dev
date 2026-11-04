# Couverture fonctionnelle et validation

La [matrice de qualification finale](final-qualification.md) relie les dix exigences
de l'issue #2 aux tests nommés et aux essais physiques encore nécessaires. Elle donne
l'ordre de passage C3/S3 et une préparation hors ligne des images/restaurations.
Les références historiques ci-dessous ne constituent pas une requalification sur
le SHA courant.

## Matrice issue de l’existant

| Capacité historique | Nouvelle implémentation | Vérification logicielle / matériel restant |
|---|---|---|
| Choisir un port Web Serial | `ui.attach`, `EspEngine` | Sélection physique et permission à valider |
| Retrouver un port autorisé / reconnexion | Connecter ouvre le sélecteur ; reconnexion automatique involontaire du même port et de la même identité ; aucun rejeu, blocage persistant conservé | Reconnexion automatique et blocage testés dans Chromium avec port simulé ; reprise réelle liée au SHA testé |
| UART externe et USB Serial/JTAG | Capacités distinctes : EN/BOOT externes non exposés au natif ; reset USB interne sur contrôleur Serial/JTAG reconnu | Refus natif et reset interne testés ; S3/C3 et renumérotation USB à qualifier par transport |
| Capture continue, timestamps | Événements UART binaires numérotés, timestamp source et serveur ; SSE / curseur REST | Chunks binaires, pertes source/anneau testés ; débit réel restant |
| Identification puce/flash/MAC | Mesure esptool avant enregistrement, revérification avant flash | Contrat et mocks ; RDID/MAC réels restant |
| Décodage partitions | Lecture 0xc00, magie, bornes et champs numériques | Décodeur Node ; tables réelles restant |
| Lecture arbitraire / dumps | `flash.read`, upload binaire temporaire, téléchargement authentifié, SHA-256 | Aller-retour API simulé ; dump réel restant |
| Programmation multi-images | Plan d’artefacts/adresses, vérification serveur puis agent, secteurs disjoints | Bornes, chevauchement, digest, famille testés ; écritures réelles restant |
| Manifestes ESP Web Tools | Un build correspondant, résolution de fichiers uploadés ; aucune URL distante ni effacement implicite | Manifeste/fichiers absents/traversal/famille testés ; installation physique restante |
| SHA-256 et vérification | Upload serveur + contrôle agent ; MD5 du chargeur conservé ; relecture indépendante à la demande | Intégrité tous octets et classification MD5 testées ; vérification réelle restante |
| Effacement explicite | `flash.erase` ou `erase:true` dans une programmation | Permission + grant + consentement testés ; effacement réel restant |
| Reset EN, BOOT tenu bas | Primitives distinctes, UART externe et console uniquement | Refus natif et quarantaine testés ; durées/signaux réels restant |
| Console ↔ chargeur / retour automatique | Moteur indépendant ; réouverture annoncée pouvant redémarrer la carte, sauf blocage | Gardes testées ; capture boot ROM réelle restante |
| UART binaire TX/RX | Base64 sans décodage serveur ; pas de payload TX en diagnostics | Intégrité et autorisations testées ; Improv/protocole applicatif appartient au client |
| Repères, déclencheurs conditionnels | `mark`, motif d’octets à usage unique `trigger` avec TTL/délai, `disarm` | Déclencheurs sans regex métier ; anti-collision et expiration à compléter sur matériel |
| Auto-reset Wi-Fi / `OTA ready` | Règles retirées ; les clients interprètent les événements et demandent leurs actions | Aucun scénario produit intégré au cœur |
| Accès complet temporaire | Grant opérateur 1 min / 5 min (défaut) / 30 min / 2 h, révocation, bornes côté agent + serveur | Révocation entre offre et départ testée ; ergonomie physique restante |
| File, échéance, résultats | Offer/ready/execute, pas de rejeu, états asynchrones, quarantaine, annulation distincte | Tests concurrence, expirations et pertes de connexion |
| HTTP vers ESP sur LAN | Primitive navigateur, liste d’origines privées locales, grant, réponse opaque explicite | Liste privée testée ; politiques réseau navigateur / HTTPS restant |
| Inventaire de binaires et sauvegardes | Inventaire des artefacts uploadés, SHA-256, sélection/réutilisation dans le plan manuel | REST testé ; dossiers de campagne et archives automatiques supprimés au profit des clients |
| Logs / rapports de qualification | Hors banc : clients des projets uniquement ; capture temporaire bornée au transport | Aucun rapport/verdict métier centralisé |
| Commandes manuelles et distantes | Session Web locale sans token, réservations automatiques et client Python sur la même API | Tests serveur / agent ; test Chromium en CI avec moteur simulé |
| Outils de campagne OTA, modèles Rust, sonde mémoire | Retirés du banc générique ; consultables sur les branches sources | Aucun transfert automatique vers les dépôts consommateurs |

Les fonctionnalités ne sont pas conservées par compatibilité d’interface : commandes renommées, transport WS et capture binaire remplacent les anciens scripts et endpoints. La reconnexion automatique ne sélectionne jamais une autre carte autorisée et ne rejoue aucune opération ; la quarantaine reste applicable et une déconnexion volontaire coupe les tentatives. Le motif exact d’octets remplace les expressions régulières exécutées par la page ; les clients restent libres d’appliquer leurs propres regex aux données.

## Ce qui est validé sans matériel

La suite couvre le broker, les routes HTTP, la session WebSocket, l’intégrité des transferts, une flash simulée, les permissions, la concurrence de réservation, les expirations, l’annulation demandée, les résultats tardifs, les pertes UART, les plans de flash et les manifestes. Les anciens tests de transport et de classification d’écriture sont conservés. Le test Chromium pilote le même agent JS avec un moteur simulé et vérifie que le client manuel et un client API utilisent la même infrastructure.

Le banc ne conserve aucune preuve de campagne durable. Les résultats automatisés logiciels appartiennent à la CI ; ils ne constituent pas une recette ESP32. Les résultats d’une qualification matérielle doivent être produits dans les projets consommateurs.

## Recette matérielle restant à réaliser

1. ESP32-S3 avec pont UART : ouverture, capture ROM, reset EN, maintien BOOT et retour console après esptool.
2. ESP32-S3 avec USB Serial/JTAG : identification/programmation/relecture, disparition en OTG, redécouverte et refus EN/BOOT.
3. ESP32-C3 : détection de la famille, flash, partitions et particularités de reset.
4. Binaires multi-images et manifeste réel : comparer les dumps SHA-256, vérifier qu’aucun effacement global n’a été inféré.
5. Débranchement pendant chaque phase de programmation ; onglet fermé/rechargé ; reprise réseau et serveur ; absence de rejeu et blocage avant tout boot automatique.
6. Mise en arrière-plan longue, débit UART soutenu, détection des lacunes et absence de faux succès sur timeout.
7. Capture de plusieurs Mo par un client SSE externe avec archivage côté projet ; reprise au curseur après interruption.
8. Autorisations et révocations depuis le navigateur réel, expiration des réservations et déclencheurs pendant une opération longue.
9. HTTP LAN sur les navigateurs retenus : accès réseau local, contenu mixte, redirections refusées et résultat opaque explicitement non vérifié.
10. Apparence et interactions de l’UI sur écran mobile et bureau ; support Web Serial déterminé par la présence effective de `navigator.serial` et les permissions du contexte, sans exclusion arbitraire d’une famille de navigateurs.

Cette session ne réalise aucun nouvel essai physique. Les retours de recette sur d548dab portent sur une S3 N16 par pont UART externe et doivent conserver leurs limites : ils ne qualifient ni les présentes corrections, ni les écritures, ni les autres interfaces/cartes. L’observation de redémarrages répétés reste à investiguer ; aucun correctif causal n’est revendiqué.

La session locale automatique et la libération après `settled` sont vérifiées par tests HTTP/broker et parcours Chromium simulés. Les autorisations locales par opération ne modifient pas les grants des clients externes. Les retours matériels antérieurs ne couvrent pas ce nouveau parcours de session.

# Protocole P4-ROM-MD5 : MD5 de la ROM, sans stub (préparation logicielle)

**Statut : mécanisme, simulation et protocole préparés. NON déployé, NON exécuté sur matériel.** Un essai exige : revue du SHA, déploiement contrôlé (le changement touche `bench/web` et `bench/protocol` : redémarrage du serveur et rechargement de la page), puis un consentement explicite de l'opérateur nommant carte, scénario, commit et effets. Aucun changement de stub, aucune sauvegarde, aucune lecture d'octets.

## 1. Question posée

Les MD5 de la même plage (`0x2000`, 4096 octets) sont-ils stables quand le **chemin de lecture du stub est retiré** ? Pour une flash de plus de 16 Mio, le stub lit par sa propre lecture rapide 4 octets (fonction ROM `esp_rom_opiflash_exec_cmd_eco7`) ; la ROM calcule `SPI_FLASH_MD5` par sa propre routine. Deux implémentations pour une même mémoire.

| Résultat | Lecture (hypothèse, jamais une cause) |
|---|---|
| MD5 ROM stable, MD5 du stub variable (PC-7 v1 : trois valeurs) | forte suspicion du chemin de lecture propre au stub, ou de son initialisation |
| MD5 ROM variable | cause commune probable : configuration SPI, alimentation, matériel |
| MD5 ROM stable, stub stable | rien à expliquer à cette étape |
| Échec du MD5 ROM | instruire l'initialisation ROM avant toute conclusion |

Des MD5 ROM et stub différents ne prouvent pas un défaut à eux seuls : seule leur reproductibilité sur la même plage compte. Un MD5 ne dit pas quels octets diffèrent.

## 2. Mécanisme (code préparé)

- **Protocole** : `flash.md5` accepte `loader` : `stub` (défaut, comportement inchangé) ou `rom`. Toute autre valeur est refusée ; `flash.read` et les autres commandes n'ont pas cet argument. `return_console=false` **ne signifie pas sans stub**.
- **Agent (`esp-engine.js`)** : en mode `rom`, `openLoader('rom')` remplace, sur cette instance d'esptool-js seulement, l'étape `runStub` par un no-op. Vérifié dans le bundle d'esptool-js 0.7.0 (version épinglée par `ui.js`) : `main()` appelle `this.runStub()` puis, si le baud diffère, `changeBaud()`, puis `readFlashId()`. Le reste est inchangé : détection de la puce, `postConnect` (séquence d'alimentation flash ECO7), baud ROM 115200 (donc pas de changement de baud), puis `SPI_ATTACH` avec la charge utile de **8 octets** d'esptool Python (le `flashSpiAttach` d'esptool-js n'en envoie que 4).
- **Refus** : une session déjà ouverte dans l'autre mode (`loader_mode_mismatch`), un stub déjà actif sur la puce détecté à la synchronisation (`rom_mode_stub_running`), un échec du rattachement (`rom_flash_attach_failed`) : aucune mesure n'est faite.
- **Contrôle d'identité inchangé et strict** : l'identifiant JEDEC est relu à chaque commande, rang compté ; un échec garde la valeur brute et son rang (comme PC-8). Aucun contournement, cache ni retry.
- **Réponse** : `md5`, `format` (`rom-ascii32` : 32 caractères ASCII, validés ; jamais un MD5 par défaut), `loader` (mode réellement utilisé), `identify.loader_stub` (0 sans stub).

## 3. Preuve qu'aucun stub n'a tourné (par mesure)

Trois indices indépendants, tous exigés : le format de réponse ROM (32 caractères ASCII, pas les 16 octets bruts du stub), `loader == "rom"` et `loader_stub == 0` dans l'identification. Un seul indice contraire arrête la recette (FAIL), après la première mesure concernée.

## 4. Séquence de la recette `P4-ROM-MD5` (PC-9)

1. MD5 ROM, `return_console=false`, première commande de la session (rang 1).
2. MD5 ROM, `return_console=true`, **deuxième commande de la même session** (rang 2) ; la console revient.
3. **Seulement si 1 et 2 sont égaux** : MD5 ROM après une **nouvelle entrée** en mode téléchargement (rang 1), `return_console=true`.

Plage fixe `0x2000` / 4096. Préconditions : consentement `P4-ROM-MD5` écrit par l'opérateur, assets de l'agent égaux à `integrity.expected_assets_sha256`, `flash.md5` annoncée, P4 32 Mio, racine de preuves persistante. Un échec en cours de route : retour console unique (sortie anticipée), aucune relance. PASS seulement si les trois MD5 sont identiques et les rangs `[1, 2, 1]` (ligne ROM-A). FAIL : opération en échec, mesure non ROM, ou MD5 différents (ROM-B si 1 ≠ 2, ROM-C si 3 ≠ 1). Un PASS ne dit rien des octets, n'autorise pas PC-BACKUP.

Si la deuxième commande échoue à l'identification (comme en PC-8), la recette s'arrête et la valeur brute devient la mesure : ce serait alors un défaut d'identification propre aussi au mode ROM, donc indépendant du stub.

## 5. Contrôles logiciels (simulation)

- Node `tests/rom-md5.test.mjs` : `openLoader` réel de l'engine, esptool-js et le port simulés ; `main()` simulé appelle `runStub()` comme le vrai. Vérifie : `runStub` jamais appelé en mode ROM, baud 115200, `SPI_ATTACH` de 8 octets une fois par session, identité stricte et rang, valeur brute d'un échec, refus des sessions de mode différent, réponses ROM mal formées, enchaînement avec retour console.
- Python `python/tests/test_pc_rom_md5.py` : validation du protocole, analyse pure (lignes ROM-A à ROM-D), recette contre le simulateur (stable, ROM-B, ROM-C, échec d'identification à la 2e commande avec valeur brute, stub chargé malgré la demande, session ROM impossible, réponse invalide, consentements distincts, cibles, assets, stockage).
- Ils ne prouvent rien sur la vraie ROM du P4, sur esptool-js réel, sur le CH343 ni sur la flash.

## 6. Limites et points à relire

- **Hypothèses sur le code d'esptool-js 0.7.0** (surcharge de `runStub` sur l'instance) : vérifiées par lecture du bundle, pas observées sur la carte.
- **Avant le rattachement**, `main()` lit encore un identifiant JEDEC par registres SPI1 (résultat seulement journalisé par esptool-js, ne bloque que s'il lève une exception). Le contrôle strict de l'agent relit ensuite l'identifiant après le rattachement.
- **`SPI_SET_PARAMS` n'est pas envoyé** (esptool Python l'envoie après le rattachement pour fixer la taille ; esptool-js n'a pas d'aide pour cela). La ROM garde donc sa taille par défaut ; la plage est sous 16 Mio, mais l'effet n'est pas observé.
- **Au-delà de 16 Mio** la ROM n'adresse pas complètement (note d'esptool) : seule cette plage est comparable ; ne pas extrapoler à la sauvegarde de 32 Mio.
- **Bogue ECO7 « deuxième entrée en mode téléchargement »** (commentaire d'esptool) : la mesure 3 (nouvelle entrée) peut échouer sans que ce soit le sujet ; pas de relance automatique.
- **Un MD5 sans octets** : aucune comparaison binaire ; elle ne remplace pas une lecture fiable.
- **Déploiement** : nouvelle empreinte des assets et du serveur ; la page doit être rechargée et le port reconnecté.

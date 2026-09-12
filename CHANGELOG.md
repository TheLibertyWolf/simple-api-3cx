# Journal des changements

Les changements visibles par les utilisateurs sont consignés ici. Le projet n’a pas encore de version publiée ni de tag de release ; les dates ci-dessous correspondent aux mises en place dans le dépôt.

## Non publié

- URL d’automatisation paramétrable pour les statuts et la connexion/déconnexion d’une file précise, avec clé révocable et filtrage IP.
- Alias courts pour les actions d’automatisation : <code>dispo</code>, <code>absent</code>, <code>npd</code>, <code>co</code> et <code>deco</code>.
- Documentation GitHub réorganisée : installation, dépendances, architecture, sécurité et référence générique des URL de l’API.
- Licence MIT, politique de sécurité et vérifications automatiques des tests Python et des scripts shell.

## 2026-09-12 — Première version opérationnelle

### Ajouté

- Installation en une commande sur le serveur 3CX : service systemd, route HTTPS dans nginx, configuration et clé administrateur initiale.
- Lecture des utilisateurs, profils actifs, files, agents et statistiques de files.
- Changement des statuts des postes et connexion/déconnexion globale aux files avec les codes SIP 3CX.
- Connexion/déconnexion **individuelle** d’un membre d’une file via la bibliothèque locale 3CX et un contrôleur .NET 10.
- Clés API avec droits de lecture et de contrôle, filtrage des IP, menu d’administration.
- Liens navigateur temporaires à usage unique et liens permanents révocables pour les statuts ou une file précise.

### Vérifié

- Installation de référence : Debian 12 et 3CX v20 PRO.
- Déconnexion puis reconnexion d’une file pour un poste membre de plusieurs files, sans changement de ses autres files.

### Points de compatibilité

- Les lectures de rapports dépendent des tables et fonctions internes de 3CX ; contrôler leur comportement après une mise à jour du PBX.
- Le contrôleur individuel est compilé contre la DLL 3CX présente sur la machine d’installation.

# Chatbot EnergiePlus

Service FastAPI séparé pour l'assistant énergie. Il est l'unique intermédiaire entre le front-end, OpenAI et Spring Boot. Il répond aux visiteurs selon le catalogue, le profil énergie et la région belge (Bruxelles-Capitale, Wallonie ou Flandre) transmis par Spring Boot.

## Démarrage

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Ensuite, démarrez le service avec :

```powershell
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8081
```

Le front-end devra appeler `http://localhost:8081/chat`. La clé `OPENAI_API_KEY` ne doit être présente que dans `.env`.

## Configuration de production

- Copiez `.env.example` en `.env` pour le développement ; en production, créez les mêmes variables dans le gestionnaire de secrets de l'hébergeur.
- Utilisez une clé OpenAI créée pour votre projet et une valeur `AI_API_KEY` longue, aléatoire et identique à celle attendue par Spring Boot.
- Remplacez `FRONTEND_ORIGINS` par le domaine HTTPS réel du front-end. Ne laissez jamais une clé dans le code, dans Git, ni dans une variable `VITE_*`.
- Sans clé OpenAI, le chatbot fonctionne en mode de secours (navigation et confirmations) mais n'a pas de réponse conversationnelle générée par IA.

## Parcours frontend

Le même endpoint `POST /chat` gère les questions libres, les simulations et les rendez-vous. La réponse contient toujours `reply`, `conversationId` et `actions`. Le frontend affiche les actions reçues : navigation, choix de région, ouverture de formulaire ou confirmation.

### Question d'un visiteur

```json
{"visiteurId": 12, "conversationId": 4, "message": "Quelle solution convient à une maison en Wallonie ?"}
```

### Simulation du produit sélectionné

Le premier appel demande la confirmation :

```json
{"visiteurId": 12, "conversationId": 4, "action": "simulation", "produitId": 8, "formulaireId": 31}
```

Après le clic de confirmation, le frontend renvoie exactement les mêmes identifiants avec `"confirme": true`. Le service accepte uniquement un produit et un formulaire présents dans le contexte Spring Boot, puis appelle le modèle de simulation déjà existant côté backend.

### Prise de rendez-vous

```json
{"visiteurId": 12, "conversationId": 4, "action": "rendez_vous", "rendezVous": {"date": "2026-10-02", "heure": "10:30"}}
```

Après confirmation, ajoutez `"confirme": true` au même objet. Spring Boot reste responsable de la disponibilité réelle du créneau et de l'enregistrement.

## Contrat Spring Boot attendu

- `POST /api/chatbot/messages` : crée le message et retourne `id` et `conversationId`.
- `GET /api/ai/chatbot/conversations/{id}/contexte` : retourne le catalogue et les formulaires/profils, avec leurs régions. Il peut aussi retourner `informationsRegionales` (ou `reglesRegionales`) pour les aides, primes et règles applicables. Sans cette donnée, l'assistant ne les invente pas.
- `POST /api/ai/chatbot/conversations/{id}/simulations` : reçoit `formulaireId` et `produitId` et utilise le modèle de simulation existant.
- `POST /api/ai/chatbot/conversations/{id}/rendez-vous` : reçoit `date` et `heure`.

## Garanties

- Les données envoyées au modèle sont réduites au catalogue et au profil énergétique nécessaire ; l'e-mail et l'adresse sont exclus.
- Une simulation ou un rendez-vous nécessite une confirmation explicite du visiteur (`confirme: true` après l'écran de confirmation).
- Spring Boot reste l'autorité qui valide et enregistre les résultats.

## Vérification

```powershell
python -m unittest -v
```

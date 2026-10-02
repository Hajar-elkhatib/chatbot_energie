"""Luna, l'assistante EnergiePlus : conseil contextuel, simulation et rendez-vous.

Le navigateur appelle ce service. Spring Boot fournit le contexte et reste
l'autorite qui valide puis enregistre les simulations et rendez-vous.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, time
from typing import Any, Literal, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

SPRING_BOOT_URL = os.getenv("SPRING_BOOT_URL", "http://localhost:8080").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "dev-ai-api-key-change-me")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.6-terra")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash")
RAG_SERVICE_URL = os.getenv("RAG_SERVICE_URL", "http://localhost:8001").rstrip("/")
BELGIAN_REGIONS = ("Bruxelles-Capitale", "Wallonie", "Flandre")
logger = logging.getLogger(__name__)

app = FastAPI(title="Chatbot EnergiePlus", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173").split(",")],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class AppointmentInput(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    heure: str = Field(pattern=r"^\d{2}:\d{2}$")


class ChatRequest(BaseModel):
    """Contrat frontend.

    ``action=chat`` est le message libre. Les boutons de l'interface utilisent
    ``simulation`` et les actions de rendez-vous sont envoyées explicitement
    par les boutons du parcours guidé.
    """

    message: str = Field(default="", max_length=2_000)
    visiteurId: Optional[int] = Field(default=None, gt=0)
    conversationId: Optional[int] = Field(default=None, gt=0)
    action: Literal["chat", "simulation", "rendez_vous", "modifier_rendez_vous", "annuler_rendez_vous"] = "chat"
    produitId: Optional[int] = Field(default=None, gt=0)
    formulaireId: Optional[int] = Field(default=None, gt=0)
    rendezVous: Optional[AppointmentInput] = None
    rendezVousId: Optional[int] = Field(default=None, gt=0)
    confirme: bool = False


def ui_action(action_type: str, **payload: Any) -> dict[str, Any]:
    """Action declarative interpretee par le frontend."""
    return {"type": action_type, **payload}


def spring_request(method: str, path: str, *, secured: bool = False, **kwargs: Any) -> Any:
    """Appelle Spring sans exposer ses details internes au visiteur."""
    headers = kwargs.pop("headers", {})
    if secured:
        headers = {**headers, "X-API-KEY": AI_API_KEY}
    try:
        response = requests.request(method, f"{SPRING_BOOT_URL}{path}", headers=headers, timeout=12, **kwargs)
    except requests.RequestException as error:
        raise HTTPException(status_code=503, detail="Le service metier EnergiePlus est indisponible.") from error
    if response.status_code == 409:
        # Un conflit de rendez-vous est une information metier attendue : le
        # visiteur doit pouvoir choisir un autre creneau, sans voir une erreur
        # technique 5xx.
        try:
            detail = response.json().get("message") or response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise HTTPException(status_code=409, detail=detail or "Ce creneau est deja reserve.")
    if response.status_code == 422:
        # Les donnees de profil peuvent etre incompletes : ce n'est pas une
        # panne du serveur et le visiteur doit etre redirige vers le formulaire.
        try:
            detail = response.json().get("message") or response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise HTTPException(status_code=422, detail=detail or "Votre profil energetique est incomplet.")
    if not response.ok:
        raise HTTPException(status_code=502, detail="Le backend EnergiePlus a refuse la demande.")
    return response.json() if response.content else None


def compact_context(context: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Le modele ne voit ni adresse, ni e-mail, ni autre donnee personnelle."""
    if not context:
        return {"catalogue": [], "formulaires": []}
    return {
        "catalogue": [
            {
                "id": item.get("id"),
                "nom": item.get("nom"),
                "type": item.get("type"),
                "prix": item.get("prix"),
                "description": item.get("description"),
            }
            for item in context.get("catalogue", [])
        ],
        "formulaires": [
            {
                "id": item.get("id"),
                "profil": item.get("profil"),
                "regions": [region.get("nom") for region in item.get("regions", [])],
                "reponses": item.get("reponses", {}),
            }
            for item in context.get("formulaires", [])[-2:]
        ],
        # Ces informations sont publiees par Spring Boot ; elles constituent la
        # seule source autorisee pour parler d'aides ou de regles regionales.
        "informationsRegionales": context.get("informationsRegionales", context.get("reglesRegionales", [])),
    }


def find_by_id(items: list[dict[str, Any]], item_id: Optional[int]) -> Optional[dict[str, Any]]:
    return next((item for item in items if item.get("id") == item_id), None)


def latest_form_id(context: dict[str, Any]) -> Optional[int]:
    forms = context.get("formulaires", [])
    return forms[-1].get("id") if forms else None


def known_regions(context: Optional[dict[str, Any]]) -> list[str]:
    """Retourne seulement les regions rattachees au profil du visiteur."""
    regions: list[str] = []
    for form in (context or {}).get("formulaires", []):
        for region in form.get("regions", []):
            name = (region.get("nom") or "").lower()
            canonical = (
                "Bruxelles-Capitale" if "brux" in name or "bruss" in name else
                "Wallonie" if "wall" in name else
                "Flandre" if "flandr" in name or "flam" in name else None
            )
            if canonical and canonical not in regions:
                regions.append(canonical)
    return regions


def region_from_message(message: str) -> Optional[str]:
    """Détecte une région uniquement lorsqu'elle est explicitement citée."""
    text = message.lower()
    if "brux" in text or "bruss" in text:
        return "Bruxelles-Capitale"
    if "wall" in text:
        return "Wallonie"
    if "flandr" in text or "vlaander" in text or "flamand" in text:
        return "Flandre"
    return None


def rag_sources_for_chat(message: str, region: str) -> list[dict[str, str]]:
    """Lit les extraits RAG locaux avant toute réponse réglementaire Gemini."""
    try:
        response = requests.post(
            f"{RAG_SERVICE_URL}/query-rag",
            json={"query": message, "region": region},
            timeout=10,
        )
        response.raise_for_status()
        return [
            {"source": str(item.get("source", "source régionale")), "extrait": str(item.get("content", ""))[:700]}
            for item in response.json().get("results", [])
            if item.get("content")
        ][:3]
    except requests.RequestException as error:
        logger.warning("RAG chatbot indisponible : %s", type(error).__name__)
        return []


def concise_reply(text: str) -> str:
    """Garde le dialogue agréable : deux phrases courtes au maximum."""
    normalized = " ".join(str(text).split())
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", normalized) if sentence.strip()]
    result = " ".join(sentences[:2]) or normalized
    if len(result) <= 260:
        return result
    clipped = result[:257].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{clipped}…"


def execute_simulation(
    conversation_id: Optional[int], context: dict[str, Any], product_id: Optional[int], form_id: Optional[int], confirmed: bool
) -> tuple[str, list[dict[str, Any]]]:
    """Utilise le modele de simulation deja existant dans Spring Boot."""
    if not conversation_id:
        return "Identifiez-vous ou enregistrez votre profil avant la simulation.", [ui_action("navigate", label="Completer mon profil", to="/formulaire")]
    product = find_by_id(context.get("catalogue", []), product_id)
    if not product:
        return "Choisissez un produit du catalogue avant de lancer une simulation.", [ui_action("navigate", label="Voir le catalogue", to="/catalogue")]
    form_id = form_id or latest_form_id(context)
    if not find_by_id(context.get("formulaires", []), form_id):
        return "Votre profil energetique est necessaire pour calculer cette estimation.", [ui_action("navigate", label="Completer mon profil", to="/formulaire", produitId=product_id)]
    if not confirmed:
        return (
            f"Vous allez lancer une simulation pour « {product.get('nom', 'ce produit')} ». Confirmez pour continuer.",
            [ui_action("confirm_simulation", produitId=product_id, formulaireId=form_id, label="Confirmer la simulation")],
        )
    try:
        result = spring_request(
            "POST",
            f"/api/ai/chatbot/conversations/{conversation_id}/simulations",
            secured=True,
            json={"formulaireId": form_id, "produitId": product_id},
        )
    except HTTPException as error:
        if error.status_code == 422:
            return "Complétez le formulaire avec la surface, la consommation, le chauffage, l’isolation, l’année du bâtiment et le prix de l’énergie. Je reprendrai ensuite la simulation.", [
                ui_action("navigate", label="Compléter mon profil énergie", to="/formulaire", formulaireId=form_id, produitId=product_id)
            ]
        raise
    return "Votre simulation est lancee.", [
        ui_action("simulation_created", referencePublique=result.get("referencePublique"), statut=result.get("statut"), produitId=product_id)
    ]


def execute_appointment(
    conversation_id: Optional[int], appointment: Optional[AppointmentInput], confirmed: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    if not conversation_id:
        return "Identifiez-vous pour enregistrer votre rendez-vous.", [ui_action("login", label="M'identifier")]
    if not appointment:
        return "Choisissez une date et une heure pour votre rendez-vous.", [ui_action("open_appointment_form", label="Choisir un creneau")]
    try:
        requested_date = date.fromisoformat(appointment.date)
        time.fromisoformat(appointment.heure)
    except ValueError:
        return "Utilisez une date AAAA-MM-JJ et une heure HH:MM.", [ui_action("open_appointment_form", label="Choisir un creneau")]
    if requested_date < date.today():
        return "Choisissez une date d'aujourd'hui ou ulterieure.", [ui_action("open_appointment_form", label="Choisir un autre creneau")]
    payload = appointment.model_dump() if hasattr(appointment, "model_dump") else appointment.dict()
    try:
        result = spring_request(
            "POST", f"/api/ai/chatbot/conversations/{conversation_id}/rendez-vous", secured=True, json=payload
        )
    except HTTPException as error:
        if error.status_code == 409:
            return f"Le créneau du {appointment.date} à {appointment.heure} vient d’être réservé par un autre visiteur. Je vous affiche les créneaux encore disponibles.", [
                ui_action("open_appointment_form", date=appointment.date, label="Voir les autres créneaux")
            ]
        raise
    return f"Votre rendez-vous du {result.get('date')} à {result.get('heure')} est enregistré.", [
        ui_action("appointment_created", rendezVousId=result.get("id"), date=result.get("date"), heure=result.get("heure")),
        ui_action("modify_appointment", rendezVousId=result.get("id"), label="Modifier le créneau"),
        ui_action("cancel_appointment", rendezVousId=result.get("id"), label="Annuler le rendez-vous"),
    ]


def modify_appointment(
    conversation_id: Optional[int], appointment_id: Optional[int], appointment: Optional[AppointmentInput]
) -> tuple[str, list[dict[str, Any]]]:
    if not conversation_id or not appointment_id or not appointment:
        return "Choisissez un nouveau jour et un nouveau créneau.", [ui_action("open_appointment_form", label="Choisir un créneau")]
    payload = appointment.model_dump() if hasattr(appointment, "model_dump") else appointment.dict()
    result = spring_request("PUT", f"/api/ai/chatbot/conversations/{conversation_id}/rendez-vous/{appointment_id}", secured=True, json=payload)
    return f"Votre rendez-vous est déplacé au {result.get('date')} à {result.get('heure')}.", [
        ui_action("modify_appointment", rendezVousId=result.get("id"), label="Modifier le créneau"),
        ui_action("cancel_appointment", rendezVousId=result.get("id"), label="Annuler le rendez-vous"),
    ]


def cancel_appointment(conversation_id: Optional[int], appointment_id: Optional[int]) -> tuple[str, list[dict[str, Any]]]:
    if not conversation_id or not appointment_id:
        return "Je ne retrouve pas ce rendez-vous.", []
    spring_request("PUT", f"/api/ai/chatbot/conversations/{conversation_id}/rendez-vous/{appointment_id}/annuler", secured=True)
    return "Votre rendez-vous est annulé.", [ui_action("appointment_cancelled")]


TOOLS = [
    {
        "type": "function",
        "name": "proposer_simulation",
        "description": "Propose une simulation pour un produit et un formulaire du contexte. Ne cree jamais de simulation.",
        "parameters": {
            "type": "object",
            "properties": {"formulaireId": {"type": "integer"}, "produitId": {"type": "integer"}},
            "required": ["formulaireId", "produitId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "proposer_rendez_vous",
        "description": "Propose le formulaire de prise de rendez-vous. Ne cree jamais de rendez-vous.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def tool_proposal(name: str, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if name == "proposer_simulation":
        product = find_by_id(context.get("catalogue", []), arguments.get("produitId"))
        form = find_by_id(context.get("formulaires", []), arguments.get("formulaireId"))
        if not product or not form:
            return {"ok": False, "message": "Produit ou profil absent du contexte autorise."}
        return {
            "ok": True,
            "message": "La simulation doit etre confirmee dans l'interface.",
            "action": ui_action("confirm_simulation", produitId=product["id"], formulaireId=form["id"], label="Lancer la simulation"),
        }
    return {"ok": True, "message": "Ouvrir le formulaire de rendez-vous.", "action": ui_action("open_appointment_form", label="Choisir un creneau")}


COMPARISON_NEEDS = (
    {"label": "Baisser ma facture", "message": "Je veux surtout baisser ma facture."},
    {"label": "Changer mon chauffage", "message": "Je veux remplacer mon chauffage."},
    {"label": "Améliorer mon confort", "message": "Je veux améliorer le confort de mon logement."},
    {"label": "Mieux utiliser mes panneaux", "message": "Je veux mieux utiliser mes panneaux solaires."},
)

ADVICE_NEEDS = (
    {"label": "Réduire ma facture", "message": "Je veux réduire ma facture d’énergie."},
    {"label": "Mieux me chauffer", "message": "Je veux mieux chauffer mon logement."},
    {"label": "Éviter les pièces froides", "message": "Je veux éviter les pièces froides."},
    {"label": "Produire mon électricité", "message": "Je veux produire mon électricité."},
)


def comparison_guidance(message: str) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """Guide court : une décision à la fois, sans rapport technique."""
    text = message.lower()
    if any(word in text for word in ("compar", "difference", "différence")):
        return (
            "Pour bien vous guider, quel est votre besoin principal ?",
            [ui_action("choose_comparison_need", choices=list(COMPARISON_NEEDS))],
        )
    if "baisser ma facture" in text:
        return (
            "Les panneaux solaires réduisent l’électricité achetée. Une batterie est utile si vous avez déjà des panneaux et souhaitez utiliser davantage votre production.",
            [ui_action("choose_product_details", products=[
                {"label": "Panneaux solaires", "to": "/produit/1"},
                {"label": "Batterie domestique", "to": "/produit/3"},
            ])],
        )
    if "remplacer mon chauffage" in text:
        return (
            "La pompe à chaleur peut remplacer le gaz ou le mazout en consommant moins d’énergie. Elle donne les meilleurs résultats dans un logement correctement isolé.",
            [ui_action("choose_product_details", products=[
                {"label": "Pompe à chaleur", "to": "/produit/4"},
            ])],
        )
    if "améliorer le confort" in text:
        return (
            "L’isolation garde la chaleur en hiver et limite les pièces froides. C’est souvent la meilleure première étape avant de changer le chauffage.",
            [ui_action("choose_product_details", products=[
                {"label": "Isolation de toiture", "to": "/produit/5"},
            ])],
        )
    if "mieux utiliser mes panneaux" in text:
        return (
            "Une batterie stocke une partie de votre électricité solaire pour l’utiliser le soir. Elle est intéressante après l’installation de panneaux solaires.",
            [ui_action("choose_product_details", products=[
                {"label": "Batterie domestique", "to": "/produit/3"},
            ])],
        )
    return None


def advice_guidance(message: str) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """Accompagne le visiteur vers une solution, sans jargon ni catalogue prématuré."""
    text = message.lower()
    if any(word in text for word in ("conseil", "accompagner", "m'aider", "m’aider")):
        return (
            "Bien sûr. Quel résultat souhaitez-vous obtenir ?",
            [ui_action("choose_advice_need", choices=list(ADVICE_NEEDS))],
        )
    if "réduire ma facture" in text:
        return (
            "Les panneaux solaires réduisent l’électricité achetée au réseau. Ils conviennent surtout si votre toiture reçoit bien le soleil.",
            [ui_action("choose_product_details", products=[
                {"label": "Découvrir les panneaux solaires", "to": "/produit/1"},
            ])],
        )
    if "mieux chauffer" in text:
        return (
            "Une pompe à chaleur peut réduire le coût du chauffage, surtout en remplacement du gaz ou du mazout. Une bonne isolation améliore son efficacité.",
            [ui_action("choose_product_details", products=[
                {"label": "Découvrir la pompe à chaleur", "to": "/produit/4"},
            ])],
        )
    if "éviter les pièces froides" in text:
        return (
            "L’isolation de la toiture limite les pertes de chaleur et rend les pièces plus confortables. C’est souvent la priorité pour un logement mal isolé.",
            [ui_action("choose_product_details", products=[
                {"label": "Découvrir l’isolation de toiture", "to": "/produit/5"},
            ])],
        )
    if "produire mon électricité" in text:
        return (
            "Les panneaux solaires transforment la lumière du soleil en électricité pour votre logement. Une batterie peut ensuite conserver une partie de cette énergie.",
            [ui_action("choose_product_details", products=[
                {"label": "Découvrir les panneaux solaires", "to": "/produit/1"},
                {"label": "Découvrir la batterie", "to": "/produit/3"},
            ])],
        )
    return None


def fallback_reply(message: str, context: Optional[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Mode local utile sans cle OpenAI ou si le modele est indisponible."""
    text = message.lower()
    regions = known_regions(context)
    comparison = comparison_guidance(message)
    if comparison:
        return comparison
    advice = advice_guidance(message)
    if advice:
        return advice
    if any(word in text for word in ("region", "prime", "aide", "subside", "brux", "wallon", "flandr")):
        if regions:
            return (
                f"Votre profil est rattache a {', '.join(regions)}. Je peux vous renseigner avec les regles et aides que le site met a disposition pour cette region.",
                [],
            )
        return (
            "Les aides, regles et estimations peuvent varier entre Bruxelles-Capitale, la Wallonie et la Flandre. Dans quelle region se situe votre projet ?",
            [ui_action("select_region", regions=list(BELGIAN_REGIONS), label="Choisir ma region")],
        )
    if any(word in text for word in ("rendez", "rdv", "appel", "visite")):
        return "Je peux preparer votre rendez-vous. Choisissez un creneau puis confirmez-le.", [ui_action("open_appointment_form", label="Prendre rendez-vous")]
    if any(word in text for word in ("simulation", "devis", "estimation", "calcul")):
        products = (context or {}).get("catalogue", [])
        if len(products) == 1:
            return "Je peux preparer une estimation pour ce produit. Vous devrez la confirmer.", [ui_action("confirm_simulation", produitId=products[0].get("id"), formulaireId=latest_form_id(context or {}), label="Preparer la simulation")]
        return "Choisissez d'abord le produit que vous souhaitez simuler.", [ui_action("choose_product", label="Choisir un produit")]
    if any(word in text for word in ("choisir", "conseil", "recommand")):
        names = ", ".join(product.get("nom", "") for product in (context or {}).get("catalogue", [])[:3]) or "nos solutions energetiques"
        return f"Je peux vous guider parmi {names}. Votre priorite est-elle les economies, l'autonomie ou le chauffage ?", [ui_action("navigate", label="Voir le catalogue", to="/catalogue")]
    return "Je peux repondre a vos questions energie, preparer une simulation ou organiser un rendez-vous. Quel est votre projet ?", []


def ai_reply(message: str, context: Optional[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Reponse contextualisee ; le modele ne peut que proposer des actions."""
    # Gemini traite les questions anonymes sans transmettre de donnees de base
    # de donnees. Les profils identifies restent sur le parcours local tant
    # qu'un accord explicite pour l'envoi de leur contexte n'a pas ete donne.
    if GEMINI_API_KEY:
        if context is not None:
            return fallback_reply(message, context)
        try:
            region = region_from_message(message)
            comparison = comparison_guidance(message)
            if comparison:
                return comparison
            advice = advice_guidance(message)
            if advice:
                return advice
            if any(word in message.lower() for word in ("prime", "aide", "subside", "règle", "regle", "obligation", "permis", "audit")) and not region:
                return (
                    "Les règles et aides diffèrent entre la Wallonie, Bruxelles-Capitale et la Flandre. Choisissez d’abord la région de votre projet.",
                    [ui_action("select_region", regions=list(BELGIAN_REGIONS), label="Choisir ma région")],
                )
            rag_sources = rag_sources_for_chat(message, region) if region else []
            prompt = (
                "Tu es Luna, l'assistante EnergiePlus specialisee en renovation energetique en Belgique. "
                "Réponds en français avec AU MAXIMUM deux phrases et 35 mots. Utilise des mots simples. "
                "Pose une seule question à la fois. Pour une question générale, ne demande pas la région et ne parle pas de sources. "
                "Ne mentionne les sources, leur absence ou la nécessité de vérifier que si la question porte précisément sur une aide, une règle ou une obligation. "
                "Si des sources RAG sont fournies, utilise exclusivement leurs informations pour toute règle, condition, aide ou obligation. "
                "N'ajoute jamais une condition absente des sources et indique qu'elle est à vérifier si les sources ne suffisent pas. "
                "Ne donne aucun montant de prime, prix, economie ou rentabilite sans source officielle fournie. "
                "Ne cree jamais de simulation ou de rendez-vous : le visiteur doit toujours confirmer dans l'interface.\n\n"
                f"Région explicitement indiquée : {region or 'non précisée'}\n"
                f"Sources RAG : {json.dumps(rag_sources, ensure_ascii=False)}\n\nQuestion : {message}"
            )
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_CHAT_MODEL}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 110},
                },
                timeout=30,
            )
            response.raise_for_status()
            answer = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not answer:
                raise ValueError("Gemini n'a retourne aucun texte exploitable.")
            _, actions = fallback_reply(message, context)
            return concise_reply(answer), actions
        except Exception as error:
            logger.warning("Reponse Gemini indisponible : %s", type(error).__name__)
            return fallback_reply(message, context)
    if not OPENAI_API_KEY:
        return fallback_reply(message, context)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_CHAT_MODEL,
            reasoning={"effort": "low"},
            store=False,
            parallel_tool_calls=False,
            instructions=(
                "Tu es Luna, l'assistante EnergiePlus specialisee dans les solutions energetiques en Belgique. "
                "Réponds en français avec au maximum deux phrases et 35 mots, dans un langage simple. "
                "Reponds clairement et seulement selon le contexte autorise. Les decisions, aides et contraintes peuvent differer entre Bruxelles-Capitale, la Wallonie et la Flandre : "
                "utilise exclusivement la ou les regions du profil. Si aucune region n'est connue et qu'elle est necessaire, demande au visiteur de choisir parmi ces trois regions. "
                "Ne donne aucun prix, prime ou economie sans source. "
                "Tu peux proposer une simulation ou un rendez-vous mais tu ne dois jamais les creer : l'utilisateur doit confirmer dans l'interface."
            ),
            input=f"Contexte metier autorise : {json.dumps(compact_context(context), ensure_ascii=False)}\n\nQuestion : {message}",
            tools=TOOLS,
        )
        outputs: list[dict[str, str]] = []
        actions: list[dict[str, Any]] = []
        for item in response.output:
            if item.type == "function_call":
                result = tool_proposal(item.name, json.loads(item.arguments), context or {})
                if result.get("action"):
                    actions.append(result["action"])
                outputs.append({"type": "function_call_output", "call_id": item.call_id, "output": json.dumps(result, ensure_ascii=False)})
        if outputs:
            response = client.responses.create(
                model=OPENAI_CHAT_MODEL,
                store=False,
                previous_response_id=response.id,
                input=outputs,
                instructions="Informe clairement de l'action proposee et de la confirmation necessaire.",
            )
        return concise_reply(response.output_text), actions
    except Exception as error:
        logger.warning("Reponse OpenAI indisponible : %s", type(error).__name__)
        return fallback_reply(message, context)


@app.get("/")
def health() -> dict[str, Any]:
    provider = "gemini" if GEMINI_API_KEY else "openai" if OPENAI_API_KEY else "fallback"
    model = GEMINI_CHAT_MODEL if provider == "gemini" else OPENAI_CHAT_MODEL
    return {"status": "UP", "provider": provider, "model": model, "aiConfigured": provider != "fallback", "version": "1.2.0"}


@app.get("/health")
def health_alias() -> dict[str, Any]:
    """Alias pratique pour les sondes de disponibilite et le deploiement."""
    return health()


@app.post("/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    """Repond au visiteur ou execute un flux explicitement confirme."""
    message = request.message.strip()
    if request.action == "chat" and not message:
        raise HTTPException(status_code=422, detail="Le message est obligatoire pour une conversation libre.")
    if not request.visiteurId:
        if request.action != "chat":
            return {"reply": "Identifiez-vous avant de lancer une simulation ou de prendre rendez-vous.", "actions": [ui_action("login", label="M'identifier")], "conversationId": None, "mode": "anonymous"}
        reply, actions = ai_reply(message, None)
        return {"reply": reply, "actions": actions, "conversationId": None, "mode": "anonymous"}

    saved = spring_request(
        "POST", "/api/chatbot/messages", json={"visiteurId": request.visiteurId, "conversationId": request.conversationId, "message": message or request.action}
    )
    conversation_id = saved["conversationId"]
    context = spring_request("GET", f"/api/ai/chatbot/conversations/{conversation_id}/contexte", secured=True)
    if request.action == "simulation":
        reply, actions = execute_simulation(conversation_id, context, request.produitId, request.formulaireId, request.confirme)
    elif request.action == "rendez_vous":
        reply, actions = execute_appointment(conversation_id, request.rendezVous)
    elif request.action == "modifier_rendez_vous":
        reply, actions = modify_appointment(conversation_id, request.rendezVousId, request.rendezVous)
    elif request.action == "annuler_rendez_vous":
        reply, actions = cancel_appointment(conversation_id, request.rendezVousId)
    else:
        reply, actions = ai_reply(message, context)
    spring_request("POST", f"/api/ai/chatbot/messages/{saved['id']}/reponse", secured=True, json={"reponse": reply})
    return {"reply": reply, "actions": actions, "conversationId": conversation_id, "mode": "openai" if OPENAI_API_KEY else "fallback"}

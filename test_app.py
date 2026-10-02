import unittest
from unittest.mock import patch

from fastapi import HTTPException

import app


CONTEXT = {
    "catalogue": [{"id": 10, "nom": "Pompe a chaleur", "type": "chauffage"}],
    "formulaires": [{"id": 22, "profil": "maison", "regions": [{"nom": "Wallonie"}], "reponses": {}}],
}


class ChatbotWorkflowTests(unittest.TestCase):
    def test_simulation_requires_confirmation(self):
        reply, actions = app.execute_simulation(7, CONTEXT, 10, 22, False)
        self.assertIn("Confirmez", reply)
        self.assertEqual(actions[0]["type"], "confirm_simulation")
        self.assertEqual(actions[0]["produitId"], 10)

    @patch("app.spring_request", return_value={"id": 91, "referencePublique": "0f47b82e-8e63-4d58-bb30-3c3b0e00db58", "statut": "CALCULEE"})
    def test_simulation_uses_selected_product_and_form(self, request):
        _, actions = app.execute_simulation(7, CONTEXT, 10, 22, True)
        self.assertEqual(actions[0]["referencePublique"], "0f47b82e-8e63-4d58-bb30-3c3b0e00db58")
        self.assertEqual(
            request.call_args.kwargs["json"], {"formulaireId": 22, "produitId": 10}
        )

    @patch("app.spring_request", side_effect=HTTPException(status_code=422, detail="Profil incomplet"))
    def test_simulation_with_incomplete_profile_opens_form(self, request):
        reply, actions = app.execute_simulation(7, CONTEXT, 10, 22, True)
        self.assertIn("surface", reply)
        self.assertEqual(actions[0]["type"], "navigate")
        self.assertEqual(actions[0]["to"], "/formulaire")

    def test_region_is_requested_when_profile_has_none(self):
        _, actions = app.fallback_reply("Quelles aides dans ma region ?", {"catalogue": [], "formulaires": []})
        self.assertEqual(actions[0]["type"], "select_region")
        self.assertEqual(actions[0]["regions"], list(app.BELGIAN_REGIONS))

    @patch("app.spring_request", return_value={"id": 34, "date": "2026-10-02", "heure": "10:30"})
    def test_appointment_is_created_only_after_confirmation(self, request):
        appointment = app.AppointmentInput(date="2026-10-02", heure="10:30")
        _, actions = app.execute_appointment(7, appointment, True)
        self.assertEqual(actions[0]["type"], "appointment_created")
        self.assertEqual(request.call_args.args[0:2], ("POST", "/api/ai/chatbot/conversations/7/rendez-vous"))

    @patch("app.spring_request", side_effect=HTTPException(status_code=409, detail="Deja reserve"))
    def test_appointment_conflict_offers_another_slot(self, request):
        appointment = app.AppointmentInput(date="2026-10-02", heure="10:30")
        reply, actions = app.execute_appointment(7, appointment, True)
        self.assertIn("réservé", reply)
        self.assertEqual(actions[0]["type"], "open_appointment_form")


if __name__ == "__main__":
    unittest.main()

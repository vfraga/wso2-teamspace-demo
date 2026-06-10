from webapp.utils.i18n import translate

def test_translation_flow(flask_app):
    # Set session['lang'] to 'en'
    with flask_app.test_request_context():
        from flask import session
        session['lang'] = 'en'
        
        # Test standard lookup
        assert translate("nav_home") == "Dashboard"
        
        # Test formatting
        assert translate("welcome_title", name="Alice") == "Welcome, Alice!"
        
        # Test meetings_at_format
        assert translate("meetings_at_format", date="2026-06-08", time="15:00", duration="45") == "2026-06-08 at 15:00 (45 min)"
        
        # Test roles_empty_state
        assert translate("roles_empty_state") == "No roles found."
        
        # Test fallback to key if not found in both
        assert translate("non_existent_key") == "non_existent_key"

def test_translation_portuguese(flask_app):
    with flask_app.test_request_context():
        from flask import session
        session['lang'] = 'pt'

        # Test standard lookup
        assert translate("nav_home") == "Painel"

        # Test formatting
        assert translate("welcome_title", name="Bob") == "Bem-vindo, Bob!"

        # Test meetings_at_format
        assert translate("meetings_at_format", date="2026-06-08", time="15:00", duration="45") == "2026-06-08 às 15:00 (45 min)"

        # Test roles_empty_state
        assert translate("roles_empty_state") == "Nenhuma função encontrada."


def test_obo_inspector_arch_note_keys_exist(flask_app):
    """The OBO Token Inspector's architectural note is rendered with |safe
    in the template (its catalog value contains <code>/<strong> tags).
    Lock in that all 3 keys exist in both locales and that the HTML
    markup is preserved verbatim through the translate() pipeline.
    """
    with flask_app.test_request_context():
        from flask import session

        session['lang'] = 'en'
        assert translate("chat_arch_note_title") == "Architectural Note: Separation of Duties (Human vs. Agent)"
        assert "<code>*_agent</code>" in translate("chat_arch_note_p1")
        assert "<strong>" in translate("chat_arch_note_p1")
        assert "<code>act</code>" in translate("chat_arch_note_p1")
        assert "<code>*_agent</code>" in translate("chat_arch_note_p2")
        assert "<code>create_meeting</code>" in translate("chat_arch_note_p2")

        session['lang'] = 'pt'
        assert translate("chat_arch_note_title") == "Nota Arquitetural: Separação de Responsabilidades (Humano vs. Agente)"
        assert "<code>*_agent</code>" in translate("chat_arch_note_p1")
        assert "<strong>" in translate("chat_arch_note_p1")
        assert "<code>*_agent</code>" in translate("chat_arch_note_p2")
        assert "<code>create_meeting</code>" in translate("chat_arch_note_p2")
